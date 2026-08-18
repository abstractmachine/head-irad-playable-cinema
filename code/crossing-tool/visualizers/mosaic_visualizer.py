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
from styles.theme import JumpScrollBar
from tool.shortcuts import KEY_PREV_TITLE, KEY_NEXT_TITLE, KEY_PREV_ITEM, KEY_NEXT_ITEM
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.inspector import Inspector
from visualizers.components.tab_panel import TabPanel
from visualizers.components.combo_popup import add_combo_all_item, style_canonical_combo
from visualizers.components.sweep_bar import SweepBar
from visualizers.components.zoom_manager import ZoomManager
from visualizers.components.metadata_block import (
    INSPECTOR_ROW_HEIGHT,
    MetadataBlock,
    status_label_stylesheet,
    table_row_edges,
)

# Fix Qt plugin conflict with OpenCV — import PyQt5 before cv2
from PyQt5.QtCore import Qt, QEvent, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QImage, QPixmap, QWheelEvent

import cv2

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]

from typing import Optional


_CLI_PATH = Path(__file__).parent.parent / "cli.py"
_VOCAB_SORT_PREF_KEY = "mosaic_vocabulary_sort"


# ---------------------------------------------------------------------------

class _VocabRow(QWidget):
    """One clickable (word, count) row for the Vocabulary table.

    Styled with the same `table_row_edges()`/`table_key_cell_style()`/
    `table_value_cell_style()` helpers used by every other canonical
    Inspector table (e.g. `MetadataBlock`'s Info-section rows), so the gap
    spacing/divider look matches. Those helpers are used by static,
    non-interactive rows elsewhere, so hover/press highlighting is added
    here manually since nothing upstream needs it.
    """

    clicked = pyqtSignal(str)

    def __init__(self, word: str, count: int, top: str, bottom: str, parent=None) -> None:
        super().__init__(parent)
        self._word, self._top, self._bottom = word, top, bottom
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._word_lbl = QLabel(word)
        self._word_lbl.setMinimumHeight(INSPECTOR_ROW_HEIGHT)
        layout.addWidget(self._word_lbl, 1)

        self._count_lbl = QLabel(str(count))
        self._count_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._count_lbl.setMinimumHeight(INSPECTOR_ROW_HEIGHT)
        self._count_lbl.setFixedWidth(56)
        layout.addWidget(self._count_lbl, 0)

        self._set_state("idle")

    def _set_state(self, state: str) -> None:
        bg = {"idle": theme.CELL_BG, "hover": theme.BTN_HOVER, "pressed": theme.ACCENT}[state]
        fg = theme.ACCENT_TEXT if state == "pressed" else theme.TEXT
        fg_dim = theme.ACCENT_TEXT if state == "pressed" else theme.TEXT_DIM
        self._word_lbl.setStyleSheet(
            f"background: {bg}; color: {fg}; font-family: '{theme.FAMILY_MONO}';"
            f" font-size: {theme.BASE_PT}pt; font-weight: {theme.WEIGHT_MONO};"
            f" {self._top}{self._bottom}"
            f" border-right: {theme.INSPECTOR_GAP}px solid {theme.TAB_BG};"
            f" padding: 0px 0px 0px 6px;"
        )
        self._count_lbl.setStyleSheet(
            f"background: {bg}; color: {fg_dim}; font-family: '{theme.FAMILY_MONO}';"
            f" font-size: {theme.BASE_PT}pt;"
            f" {self._top}{self._bottom}"
            f" padding: 0px 6px 0px 0px;"
        )

    def enterEvent(self, event) -> None:
        self._set_state("hover")
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_state("idle")
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._set_state("pressed")
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            inside = self.rect().contains(event.pos())
            self._set_state("hover" if inside else "idle")
            if inside:
                self.clicked.emit(self._word)
        super().mouseReleaseEvent(event)


class VocabularyTable(QWidget):
    """Dynamic, clickable two-column (word | count) vocabulary list.

    Rows are rebuilt on every `set_items()` call and styled identically to
    the canonical Inspector table cells (same gap/divider look as
    MetadataBlock-based Info sections). No internal scrollbar — this widget
    sizes to its content and relies on the outer TabPanel's scroll area for
    overflow.
    """

    item_clicked = pyqtSignal(str)
    population_finished = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._rows: list = []
        self._pending_items: list = []
        self._pending_index = 0
        self._population_timer = QTimer(self)
        self._population_timer.setSingleShot(True)
        self._population_timer.timeout.connect(self._append_batch)
        self._population_deferred_layout = False

    def clear(self) -> None:
        self._population_timer.stop()
        self._finish_deferred_layout()
        self._pending_items = []
        self._pending_index = 0
        for row in self._rows:
            self._layout.removeWidget(row)
            row.deleteLater()
        self._rows = []

    def set_message(self, text: str) -> None:
        self.clear()
        top, bottom = table_row_edges(0, 0)
        lbl = QLabel(text)
        lbl.setMinimumHeight(INSPECTOR_ROW_HEIGHT)
        lbl.setStyleSheet(
            f"background: {theme.CELL_BG}; color: {theme.TEXT_DIM};"
            f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
            f" {top}{bottom} padding: 0px 6px;"
        )
        self._layout.addWidget(lbl)
        self._rows.append(lbl)

    def set_items(self, items: list, *, incremental: bool = False) -> None:
        self.clear()
        self._pending_items = list(items)
        if incremental:
            self._population_deferred_layout = True
            self.setUpdatesEnabled(False)
            self.setVisible(False)
            self._layout.setEnabled(False)
            self._population_timer.start(0)
        else:
            self._append_batch(len(self._pending_items))

    def _finish_deferred_layout(self) -> None:
        if not self._population_deferred_layout:
            return
        self._population_deferred_layout = False
        self._layout.setEnabled(True)
        self.setVisible(True)
        self.setUpdatesEnabled(True)
        self.updateGeometry()
        self.update()

    def _append_batch(self, batch_size: int = 40) -> None:
        last_idx = len(self._pending_items) - 1
        stop = min(self._pending_index + batch_size, len(self._pending_items))
        for row_idx in range(self._pending_index, stop):
            entry = self._pending_items[row_idx]
            top, bottom = table_row_edges(row_idx, last_idx)
            row = _VocabRow(entry["value"], entry["count"], top, bottom)
            row.clicked.connect(self.item_clicked)
            self._layout.addWidget(row)
            self._rows.append(row)
        self._pending_index = stop
        if self._pending_index < len(self._pending_items):
            self._population_timer.start(0)
        else:
            self._pending_items = []
            self._finish_deferred_layout()
            self.population_finished.emit()


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

VOCABULARY_RENDER_THRESHOLD = 200

DEFAULT_TILE_SIZE = 200
MIN_TILE_SIZE = 80
MAX_TILE_SIZE = 480
ZOOM_STEP = 24
_ZOOM_PREF_KEY = "mosaic_tile_zoom"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOVIE_TITLE_YEAR_RE = re.compile(r"^(?P<title>.*)\s\((?P<year>[^()]+)\)$")


def _split_movie_title_year(movie_title: str) -> "tuple[str, str]":
    """Split a search result's ``movie_title`` into ``(title, year)``.

    Mirrors the exact formatting produced by ``services.search.search_shots()``:
    ``f"{title} ({year})" if year else title``. Returns ``("", "")`` for an
    empty input and ``(movie_title, "")`` if no trailing ``(year)`` is found.
    """
    if not movie_title:
        return "", ""
    m = _MOVIE_TITLE_YEAR_RE.match(movie_title)
    if m:
        return m.group("title"), m.group("year")
    return movie_title, ""

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
        media_type: str = "movie",
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
        self.media_type      = media_type
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

            media_types = ("movie", "gameplay") if self.media_type == "--all" else (self.media_type,)
            results = []
            for media_type in media_types:
                result = search_shots(
                    query          = self.query,
                    scopes         = scopes,
                    field          = field,
                    limit          = total_limit,
                    limit_per_item = limit_pi,
                    use_all        = use_all,
                    project_path   = self.project_path,
                    media_type     = media_type,
                )
                results.extend(
                    {**item, "media_type": media_type}
                    for item in result.get("results", [])
                )
            if total_limit is not None:
                results = results[:total_limit]

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
                                media_type=r.get("media_type", self.media_type),
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
    """Fetch indexed vocabulary for one annotation field in a background thread.

    Signals
    -------
    result_ready(dict)  index status and, when ready, vocabulary items
    error(str)          emitted on failure
    """

    result_ready = pyqtSignal(dict)
    error       = pyqtSignal(str)

    def __init__(
        self,
        field: str,
        scope: Optional[str],
        project_path: str,
        media_type: str = "movie",
        prefix: Optional[str] = None,
        sort: str = "count",
        parent=None,
    ):
        super().__init__(parent)
        self.field        = field
        self.scope        = scope
        self.project_path = project_path
        self.media_type   = media_type
        self.prefix       = prefix
        self.sort         = sort

    def run(self) -> None:
        try:
            from services.search import vocabulary_from_index
            result = vocabulary_from_index(
                field        = self.field,
                show_count   = True,
                project_path = self.project_path,
                media_type   = self.media_type,
                sort         = self.sort,
            )
            if result.get("status") == "ready" and self.scope and self.scope != "--all":
                result = {"status": "scope_unsupported"}
            elif result.get("status") == "ready":
                all_items = result.get("items", [])
                initials = sorted(
                    {
                        value[:1].lower() if value[:1].isalpha() else "#"
                        for value in (str(item.get("value", "")) for item in all_items)
                        if value
                    },
                    key=lambda value: (value == "#", value),
                )
                is_large = len(all_items) > VOCABULARY_RENDER_THRESHOLD
                selected_prefix = self.prefix or "--all"
                if selected_prefix != "--all":
                    items = [
                        item for item in all_items
                        if (
                            str(item.get("value", ""))[:1].lower()
                            if str(item.get("value", ""))[:1].isalpha()
                            else "#"
                        ) == selected_prefix
                    ]
                else:
                    items = all_items
                result = {
                    "status": "ready",
                    "items": items,
                    "total": len(all_items),
                    "initials": initials,
                    "selected_prefix": selected_prefix,
                    "is_large": is_large,
                }
            self.result_ready.emit(result)
        except Exception as exc:
            import traceback
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


class VocabularyIndexWorker(QThread):
    """Rebuild the field-derived vocabulary family through the CLI command."""

    finished_signal = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, media_type: str, family: str = "canonical", parent=None) -> None:
        super().__init__(parent)
        self.media_type = media_type
        self.family = family

    def run(self) -> None:
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(_CLI_PATH),
                    "index",
                    "vocabulary",
                    "--media",
                    self.media_type,
                    "--family",
                    self.family,
                    "--force",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            output = "\n".join(
                part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
            )
            if completed.returncode != 0:
                self.error.emit(output or "Vocabulary index rebuild failed.")
                return
            self.finished_signal.emit(output)
        except Exception as exc:
            self.error.emit(str(exc))


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
    """A single mosaic tile: the raw scaled video frame, with a hover tooltip.

    Single left-click selects the tile: an accent highlight border is drawn
    around it, the Inspector's "Info" section (title/year) is populated, and
    its "Shotlist" button is enabled (see ``select_requested``).
    Double-clicking a tile immediately opens that shot in the Shotlist
    Visualizer via IPC (see ``open_requested``).
    """

    _PLACEHOLDER_BG = theme.CANVAS_BG
    _BORDER_W = 2  # accent selection-highlight border thickness (px)

    # Emitted on single-click (passes self); handled by MosaicCanvas to
    # manage which tile is highlighted, then forwarded (as a result dict)
    # to MosaicVisualizer.
    select_requested = pyqtSignal(object)
    # Emitted on double-click; forwarded through MosaicCanvas to MosaicVisualizer.
    open_requested = pyqtSignal(dict)

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
        self._selected        = False

        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_frame_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.setStyleSheet(f"background: {self._PLACEHOLDER_BG}; border: none;")
        layout.addWidget(self._img_label, 0, Qt.AlignCenter)

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
        pad = self._BORDER_W * 2
        self.setFixedSize(img_w + pad, img_h + pad)
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

    def _apply_frame_style(self) -> None:
        border_color = theme.ACCENT if self._selected else "transparent"
        self.setStyleSheet(
            f"TileWidget {{ border: {self._BORDER_W}px solid {border_color}; "
            f"background: {theme.CANVAS_BG}; }}"
        )

    def set_selected(self, selected: bool) -> None:
        """Toggle the accent selection-highlight border."""
        if self._selected != selected:
            self._selected = selected
            self._apply_frame_style()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.select_requested.emit(self)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(self.result)
        super().mouseDoubleClickEvent(event)


# ---------------------------------------------------------------------------
# Mosaic canvas — scrollable grid of tiles
# ---------------------------------------------------------------------------

class MosaicCanvas(QScrollArea):
    """Scrollable viewport containing a grid of TileWidget tiles.

    Ctrl + scroll wheel     → zoom in / out (see `ZoomManager`)
    Ctrl + '+' / Ctrl + '-' → zoom in / out
    Plain scroll wheel      → vertical scroll (default)
    """

    open_requested = pyqtSignal(dict)
    selection_changed = pyqtSignal(dict)
    selection_cleared = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setStyleSheet(f"QScrollArea {{ border: none; background: {theme.CANVAS_BG}; }}")
        self.setVerticalScrollBar(JumpScrollBar())

        from tool import prefs as _prefs
        initial_zoom = float(_prefs.get(_ZOOM_PREF_KEY, DEFAULT_TILE_SIZE) or DEFAULT_TILE_SIZE)
        self._zoom_manager = ZoomManager(
            self,
            initial_zoom,
            MIN_TILE_SIZE,
            MAX_TILE_SIZE,
            ZOOM_STEP,
            persist_cb=lambda v: _prefs.set(_ZOOM_PREF_KEY, v),
        )
        self._tiles: list[TileWidget] = []
        self._selected_tile: Optional[TileWidget] = None

        self._container = QWidget()
        self._container.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self.setWidget(self._container)

    # ------------------------------------------------------------------
    # Public API

    @property
    def tile_count(self) -> int:
        return len(self._tiles)

    @property
    def tile_size(self) -> int:
        return int(round(self._zoom_manager.zoom()))

    def clear(self) -> None:
        for tile in self._tiles:
            tile.deleteLater()
        self._tiles.clear()
        self._selected_tile = None
        self.selection_cleared.emit()
        self._container.setFixedSize(max(self.viewport().width(), 1), 1)

    def add_tile(self, result: dict, pixmap) -> None:
        tile = TileWidget(result, pixmap, self.tile_size, parent=self._container)
        tile.open_requested.connect(self.open_requested)
        tile.select_requested.connect(self._on_tile_select_requested)
        tile.show()
        self._tiles.append(tile)
        self._do_flow_layout()

    def _on_tile_select_requested(self, tile: "TileWidget") -> None:
        """A tile was clicked: highlight it (deselecting any previous tile)
        and forward its result dict to the visualizer."""
        if self._selected_tile is tile:
            return
        if self._selected_tile is not None:
            self._selected_tile.set_selected(False)
        tile.set_selected(True)
        self._selected_tile = tile
        self.selection_changed.emit(tile.result)

    # ------------------------------------------------------------------
    # Flow layout

    def _do_flow_layout(self) -> None:
        """Position all tiles with word-wrap: wrap to the next row when a
        tile's right edge would exceed the viewport width."""
        if not self._tiles:
            self._container.setFixedSize(max(self.viewport().width(), 1), 1)
            return

        spacing = theme.SECTION_GAP
        margin  = theme.SECTION_GAP
        vp_w    = self.viewport().width()

        x     = margin
        y     = margin
        row_h = 0

        for i, tile in enumerate(self._tiles):
            tw = tile.width()
            th = tile.height()

            if x > margin and x + tw > vp_w - margin:
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
        if self._zoom_manager.handle_wheel_event(event):
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._zoom_manager.handle_key_event(event, DEFAULT_TILE_SIZE):
            return
        super().keyPressEvent(event)

    def request_reflow(self) -> None:
        """Called by ZoomManager after a zoom change: re-render every tile at
        the new size and re-run the flow layout."""
        size = self.tile_size
        for tile in self._tiles:
            tile.resize_tile(size)
        self._do_flow_layout()


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
                Path(self.project_path) / "outputs" / "mosaics"
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

class MosaicVisualizer(WindowVisualizer):
    """Interactive mosaic visualizer window — canvas browser, controls in the inspector."""

    def __init__(self, project_path: str, media_type: str = "movie"):
        # Instance attributes must be set before super().__init__() since the
        # base class calls create_browser()/create_inspector() synchronously.
        self.project_path = project_path
        self.media_type   = media_type
        self._worker: Optional[SearchWorker] = None
        self._pdf_worker: Optional[PdfExportWorker] = None
        self._vocab_worker: Optional[VocabularyWorker] = None
        self._vocab_rebuild_worker: Optional[VocabularyIndexWorker] = None
        self._vocab_request_id = 0
        self._initial_vocab_load_started = False
        self._export_worker: Optional[ExportWorker] = None
        self._video_worker: Optional[VideoMosaicWorker] = None
        self._current_results: list = []   # results for the last completed search
        self.best_mode: bool = False
        self._selected_result: Optional[dict] = None   # currently-selected browser tile

        super().__init__(pref_key="window_mosaic")
        self.setWindowTitle("Mosaic")
        self.resize(1440, 900)

        self._populate_movies()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._initial_vocab_load_started:
            self._initial_vocab_load_started = True
            self._on_field_changed()

    def create_browser(self) -> QWidget:
        self.canvas = MosaicCanvas()
        self.canvas.installEventFilter(self)
        self.canvas.viewport().installEventFilter(self)
        self.canvas.open_requested.connect(self._on_tile_open_requested)
        self.canvas.selection_changed.connect(self._on_tile_selected)
        self.canvas.selection_cleared.connect(self._on_selection_cleared)

        # 1px accent progress bar sits between the canvas and its viewport edge
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
        return canvas_col

    def create_inspector(self) -> QWidget:
        panel = TabPanel()

        # ── Scope section — media type then title ───────────────────
        scope_wrap = QWidget()
        scope_layout = QVBoxLayout(scope_wrap)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_layout.setSpacing(theme.SECTION_GAP)
        self.media_type_combo = QComboBox()
        add_combo_all_item(self.media_type_combo, user_data="--all")
        self.media_type_combo.setItemText(0, "<All Media>")
        self.media_type_combo.addItems(["movie", "gameplay"])
        self.media_type_combo.setCurrentText(self.media_type)
        style_canonical_combo(self.media_type_combo)
        self.media_type_combo.currentIndexChanged.connect(
            lambda _index: self._on_media_type_changed(
                self.media_type_combo.currentData() or self.media_type_combo.currentText()
            )
        )
        self.media_type_combo.installEventFilter(self)
        scope_layout.addWidget(self.media_type_combo)
        self.movie_combo = QComboBox()
        add_combo_all_item(self.movie_combo)
        self.movie_combo.setItemText(0, "<All Titles>")
        style_canonical_combo(self.movie_combo)
        self.movie_combo.currentIndexChanged.connect(self._on_field_changed)
        self.movie_combo.installEventFilter(self)
        scope_layout.addWidget(self.movie_combo)
        panel.add_section("Scope", scope_wrap, pref_key="mosaic_section_scope")

        # ── Field section ──────────────────────────────────────────────
        field_wrap = QWidget()
        field_layout = QVBoxLayout(field_wrap)
        field_layout.setContentsMargins(0, 0, 0, 0)
        field_layout.setSpacing(theme.SECTION_GAP)
        self.field_combo = QComboBox()
        for f in ANNOTATION_FIELDS:
            if f == "--all":
                add_combo_all_item(self.field_combo, user_data="--all")
                self.field_combo.setItemText(self.field_combo.count() - 1, "<All Fields>")
            else:
                self.field_combo.addItem(f, userData=f)
        style_canonical_combo(self.field_combo)
        self.field_combo.currentIndexChanged.connect(self._on_field_changed)
        self.field_combo.installEventFilter(self)
        field_layout.addWidget(self.field_combo)
        panel.add_section("Field", field_wrap, pref_key="mosaic_section_field")

        # ── Search section — query text + action button grid ─────────
        query_wrap = QWidget()
        query_layout = QVBoxLayout(query_wrap)
        query_layout.setContentsMargins(0, 0, 0, 0)
        query_layout.setSpacing(theme.SECTION_GAP)
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("e.g. gun,  sunset,  rain…")
        self.query_input.returnPressed.connect(self._on_search)
        self.query_input.textChanged.connect(self._update_search_button)
        query_layout.addWidget(self.query_input)

        btn_grid = QGridLayout()
        btn_grid.setContentsMargins(0, 0, 0, 0)
        btn_grid.setSpacing(theme.SECTION_GAP)

        self.search_btn = QPushButton("Search")
        self.search_btn.setStyleSheet(theme.action_button_stylesheet())
        self.search_btn.setFocusPolicy(Qt.NoFocus)
        self.search_btn.setEnabled(False)
        self.search_btn.setToolTip("Search shot annotations for the query term")
        self.search_btn.clicked.connect(self._on_search)
        btn_grid.addWidget(self.search_btn, 0, 0)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setStyleSheet(theme.action_button_stylesheet())
        self.clear_btn.setFocusPolicy(Qt.NoFocus)
        self.clear_btn.setEnabled(False)
        self.clear_btn.setToolTip("Clear the current Mosaic browser results")
        self.clear_btn.clicked.connect(self._on_clear)

        self.best_btn = QPushButton("Best")
        self.best_btn.setStyleSheet(theme.action_button_stylesheet())
        self.best_btn.setFocusPolicy(Qt.NoFocus)
        self.best_btn.setCheckable(True)
        self.best_btn.setEnabled(False)
        self.best_btn.setToolTip("Show CLIP-scored best frame for each matched shot")
        self.best_btn.clicked.connect(self._on_best_toggle)
        btn_grid.addWidget(self.best_btn, 0, 1)

        self.shotlist_btn = QPushButton("Shotlist")
        self.shotlist_btn.setStyleSheet(theme.action_button_stylesheet())
        self.shotlist_btn.setFocusPolicy(Qt.NoFocus)
        self.shotlist_btn.setEnabled(False)
        self.shotlist_btn.setToolTip("Open the selected shot in the Shotlist Visualizer")
        self.shotlist_btn.clicked.connect(self._on_shotlist_btn_clicked)
        btn_grid.addWidget(self.shotlist_btn, 1, 0)

        btn_grid.addWidget(self.clear_btn, 1, 1)

        self.pdf_btn = QPushButton("PDF")
        self.pdf_btn.setStyleSheet(theme.action_button_stylesheet())
        self.pdf_btn.setFocusPolicy(Qt.NoFocus)
        self.pdf_btn.setEnabled(False)
        self.pdf_btn.setToolTip("Export a PDF contact sheet of the current results")
        self.pdf_btn.clicked.connect(self._on_export_pdf)
        btn_grid.addWidget(self.pdf_btn, 2, 0)

        self.video_btn = QPushButton("Video")
        self.video_btn.setStyleSheet(theme.action_button_stylesheet())
        self.video_btn.setFocusPolicy(Qt.NoFocus)
        self.video_btn.setEnabled(False)
        self.video_btn.setToolTip(
            "Generate a looping video mosaic (.mp4) from the current results\n"
            "saved to outputs/mosaics/video/search/"
        )
        self.video_btn.clicked.connect(self._on_save_video)
        btn_grid.addWidget(self.video_btn, 2, 1)

        query_layout.addLayout(btn_grid)

        self.search_status_label = QLabel("Enter a query and press Search")
        self.search_status_label.setWordWrap(True)
        self.search_status_label.setStyleSheet(status_label_stylesheet())
        query_layout.addWidget(self.search_status_label)

        panel.add_section("Search", query_wrap, pref_key="mosaic_section_search")

        # ── Info section — title/year of the currently-selected frame ──
        info_wrap = QWidget()
        info_layout = QVBoxLayout(info_wrap)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        self.info_block = MetadataBlock(["title", "year"])
        info_layout.addWidget(self.info_block)
        panel.add_section("Info", info_wrap, pref_key="mosaic_section_info")

        # ── Options section ───────────────────────────────────────────
        opt_wrap = QWidget()
        opt_layout = QVBoxLayout(opt_wrap)
        opt_layout.setContentsMargins(0, 0, 0, 0)
        opt_layout.setSpacing(theme.SECTION_GAP)

        limit_row = QHBoxLayout()
        limit_row.setSpacing(4)
        lim_label = QLabel("Limit:")
        lim_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        limit_row.addWidget(lim_label)
        self.limit_combo = QComboBox()
        for opt in ["1", "5", "10", "50", "200", "all"]:
            self.limit_combo.addItem(opt)
        self.limit_combo.setCurrentText("50")
        style_canonical_combo(self.limit_combo)
        limit_row.addWidget(self.limit_combo)
        opt_layout.addLayout(limit_row)

        self.limit_per_movie_cb = QCheckBox("Limit per movie")
        self.limit_per_movie_cb.setFixedHeight(theme.BTN_H)
        self.limit_per_movie_cb.setFocusPolicy(Qt.NoFocus)
        self.limit_per_movie_cb.setStyleSheet(
            f"QCheckBox {{ color: {theme.TEXT};"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; padding: 0px; }}"
            f"QCheckBox::indicator {{ width: {theme.BTN_ICON}px; height: {theme.BTN_ICON}px; }}"
        )
        opt_layout.addWidget(self.limit_per_movie_cb)

        fps_row = QHBoxLayout()
        fps_row.setSpacing(4)
        fps_label = QLabel("FPS:")
        fps_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        fps_row.addWidget(fps_label)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 30)
        self.fps_spin.setValue(8)
        fps_row.addWidget(self.fps_spin)
        opt_layout.addLayout(fps_row)

        dur_row = QHBoxLayout()
        dur_row.setSpacing(4)
        dur_label = QLabel("Dur (s):")
        dur_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        dur_row.addWidget(dur_label)
        self.dur_spin = QSpinBox()
        self.dur_spin.setRange(1, 10)
        self.dur_spin.setValue(2)
        dur_row.addWidget(self.dur_spin)
        opt_layout.addLayout(dur_row)

        panel.add_section("Options", opt_wrap, pref_key="mosaic_section_options")

        # ── Tools section — browser zoom controls ────────────────────
        tools_wrap = QWidget()
        tools_layout = QVBoxLayout(tools_wrap)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        tools_layout.setSpacing(theme.SECTION_GAP)

        zoom_grid = QGridLayout()
        zoom_grid.setContentsMargins(0, 0, 0, 0)
        zoom_grid.setSpacing(theme.SECTION_GAP)

        self.zoom_in_btn = QPushButton("Zoom +")
        self.zoom_in_btn.setStyleSheet(theme.action_button_stylesheet())
        self.zoom_in_btn.setFocusPolicy(Qt.NoFocus)
        self.zoom_in_btn.setToolTip("Enlarge mosaic tiles")
        self.zoom_in_btn.clicked.connect(lambda: self._change_zoom(ZOOM_STEP))
        zoom_grid.addWidget(self.zoom_in_btn, 0, 0)

        self.zoom_out_btn = QPushButton("Zoom -")
        self.zoom_out_btn.setStyleSheet(theme.action_button_stylesheet())
        self.zoom_out_btn.setFocusPolicy(Qt.NoFocus)
        self.zoom_out_btn.setToolTip("Shrink mosaic tiles")
        self.zoom_out_btn.clicked.connect(lambda: self._change_zoom(-ZOOM_STEP))
        zoom_grid.addWidget(self.zoom_out_btn, 0, 1)

        tools_layout.addLayout(zoom_grid)

        self.vocab_rebuild_btn = QPushButton("Rebuild Vocabulary")
        self.vocab_rebuild_btn.setStyleSheet(theme.action_button_stylesheet())
        self.vocab_rebuild_btn.setFocusPolicy(Qt.NoFocus)
        self.vocab_rebuild_btn.setToolTip(
            "Rebuild the vocabulary index from project annotations"
        )
        self.vocab_rebuild_btn.setEnabled(False)
        self.vocab_rebuild_btn.clicked.connect(self._on_vocab_rebuild)
        tools_layout.addWidget(self.vocab_rebuild_btn)

        panel.add_section("Tools", tools_wrap, pref_key="mosaic_section_tools")

        # ── Vocabulary section — two-column (word, count) table sized to
        # its content; the outer TabPanel's scroll area handles overflow
        # scrolling for the whole tab rather than this section owning its
        # own internal scrollbar.
        vocab_wrap = QWidget()
        vocab_layout = QVBoxLayout(vocab_wrap)
        vocab_layout.setContentsMargins(0, 0, 0, 0)
        vocab_layout.setSpacing(theme.SECTION_GAP)
        self.vocab_sort_combo = QComboBox()
        self.vocab_sort_combo.addItem("Count", userData="count")
        self.vocab_sort_combo.addItem("Alphabetical", userData="alphabetical")
        self.vocab_sort_combo.addItem(
            "Count + Alphabetical", userData="count_alphabetical"
        )
        style_canonical_combo(self.vocab_sort_combo)
        from tool import prefs as _prefs
        saved_sort = _prefs.get(_VOCAB_SORT_PREF_KEY, "count")
        saved_index = self.vocab_sort_combo.findData(saved_sort)
        if saved_index >= 0:
            self.vocab_sort_combo.setCurrentIndex(saved_index)
        self.vocab_sort_combo.setToolTip("Sort vocabulary values")
        self.vocab_sort_combo.currentIndexChanged.connect(self._on_vocab_sort_changed)
        vocab_layout.addWidget(self.vocab_sort_combo)
        self.vocab_nav_combo = QComboBox()
        style_canonical_combo(self.vocab_nav_combo)
        self.vocab_nav_combo.setToolTip("Show vocabulary values by initial letter")
        self.vocab_nav_combo.currentIndexChanged.connect(self._on_vocab_nav_changed)
        self.vocab_nav_combo.setVisible(False)
        vocab_layout.addWidget(self.vocab_nav_combo)
        self.vocab_table = VocabularyTable()
        self.vocab_table.item_clicked.connect(self._on_vocab_item_clicked)
        self.vocab_table.population_finished.connect(self._on_vocab_population_finished)
        vocab_layout.addWidget(self.vocab_table)
        vocab_sec = panel.add_section("Vocabulary", vocab_wrap, pref_key="mosaic_section_vocab")

        # Accent sweep-bar shown on the section title while the vocabulary is
        # loading in the background — same loading behavior as the other
        # canonical visualizers, visible even when the section is collapsed.
        self._vocab_loading_bar = SweepBar(self)
        self._vocab_loading_timer = QTimer(self)
        self._vocab_loading_timer.setInterval(20)   # ~50 fps
        self._vocab_loading_timer.timeout.connect(self._vocab_loading_bar.tick)
        vocab_sec.set_subbar(self._vocab_loading_bar)

        self._inspector = Inspector()
        self._inspector.add_tab(panel, "Mosaic")
        self._update_zoom_buttons()
        return self._inspector

    # ------------------------------------------------------------------
    # Control panel handlers

    def _on_btn_open_visualizer(self, result: dict) -> None:
        """Open the shot in the Shotlist Visualiser (via IPC)."""
        filename = result.get("filename") or result.get("movie_title", "")
        shot_id  = str(result.get("shot_id", ""))
        if not filename:
            return

        from visualizers.shot_visualizer import open_at_shot
        open_at_shot(self.project_path, filename, result.get("media_type", self.media_type), shot_id=shot_id,
                     loop=True, no_continue=True, play=True)

    def _on_tile_selected(self, result: dict) -> None:
        """Single-click selection in the browser: highlight the tile (handled by
        MosaicCanvas), enable the Shotlist button, and populate the Info
        section's title/year for the selected frame."""
        self._selected_result = result
        self.shotlist_btn.setEnabled(True)
        title, year = _split_movie_title_year(result.get("movie_title", ""))
        self.info_block.load({"title": title or "—", "year": year or "—"})

    def _on_selection_cleared(self) -> None:
        """No tile selected (e.g. canvas cleared for a new search): reset
        the Shotlist button and the Info section back to their empty state."""
        self._selected_result = None
        self.shotlist_btn.setEnabled(False)
        self.info_block.clear()

    def _on_tile_open_requested(self, result: dict) -> None:
        """Double-click on a tile: open it immediately in the Shotlist Visualizer.

        The preceding single-click already selected this tile (see
        `_on_tile_selected`), so the Info section and Shotlist button are
        already in sync before this fires.
        """
        self._on_btn_open_visualizer(result)

    def _on_shotlist_btn_clicked(self) -> None:
        """Inspector "Shotlist" button: re-open the currently-selected shot."""
        if self._selected_result:
            self._on_btn_open_visualizer(self._selected_result)

    def _on_media_type_changed(self, media_type: str) -> None:
        """Called when the media type combo changes."""
        self.media_type = media_type
        self.canvas.clear()
        self._current_results = []
        self._update_result_controls()
        self._update_best_button()
        self.movie_combo.blockSignals(True)
        self.movie_combo.clear()
        add_combo_all_item(self.movie_combo)
        self.movie_combo.setItemText(0, "<All Titles>")
        self.movie_combo.blockSignals(False)
        self._populate_movies()

    def _current_scope(self) -> "tuple[str, str | None]":
        """Return (media_type, filename_or_none) for the current scope selection."""
        filename = self.movie_combo.currentData()
        if isinstance(filename, tuple):
            return filename
        return self.media_type, filename if filename else None

    def _select_scope(self, media_type: str, filename: "str | None") -> None:
        """Programmatically select a scope without triggering signals."""
        self.media_type_combo.blockSignals(True)
        index = self.media_type_combo.findData(media_type)
        self.media_type_combo.setCurrentIndex(index if index >= 0 else self.media_type_combo.findText(media_type))
        self.media_type = media_type
        self.media_type_combo.blockSignals(False)
        # Repopulate titles for this type, then select the right entry
        self._populate_movies()
        if filename is not None:
            idx = self.movie_combo.findData(filename)
            if idx >= 0:
                self.movie_combo.setCurrentIndex(idx)

    def _populate_movies(self) -> None:
        """Populate movie_combo from project metadata for the current media type."""
        try:
            from data.metadata import get_metadata
            media_types = ("movie", "gameplay") if self.media_type == "--all" else (self.media_type,)
            rows = [
                (media_type, row)
                for media_type in media_types
                for row in get_metadata(self.project_path, media_type=media_type)
            ]
            sorted_rows = sorted(rows, key=lambda item: (item[1].get("title") or "").lower())
            for media_type, row in sorted_rows:
                title    = row.get("title", "")
                year     = row.get("year", "")
                label    = f"{title} ({year})" if year else title
                filename = row.get("filename", "")
                if label and filename:
                    if self.media_type == "--all":
                        self.movie_combo.addItem(f"{label} [{media_type}]", userData=(media_type, filename))
                    else:
                        self.movie_combo.addItem(label, userData=filename)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Best-mode helpers

    def _update_search_button(self) -> None:
        has_query = bool(self.query_input.text().strip())
        self.search_btn.setEnabled(has_query)
        self._update_best_button()

    def _update_best_button(self) -> None:
        """Best is available once there's a query to search for, or once the
        browser already has results to re-render in best-frame mode."""
        has_query   = bool(self.query_input.text().strip())
        has_results = bool(self._current_results)
        self.best_btn.setEnabled(has_query or has_results)

    def _update_result_controls(self) -> None:
        has_results = bool(self._current_results)
        self.clear_btn.setEnabled(has_results)
        self.pdf_btn.setEnabled(has_results)
        self.video_btn.setEnabled(has_results)
        self._update_best_button()

    def _on_best_toggle(self) -> None:
        self.best_mode = self.best_btn.isChecked()
        self._update_search_button()

    # ------------------------------------------------------------------
    # Zoom helpers

    def _change_zoom(self, delta: float) -> None:
        self.canvas._zoom_manager.change_zoom(delta)
        self._update_zoom_buttons()

    def _update_zoom_buttons(self) -> None:
        """Enable/disable the Zoom +/- buttons based on the canvas's zoom limits."""
        zm = self.canvas._zoom_manager
        eps = 1e-9
        self.zoom_in_btn.setEnabled(zm.zoom() < MAX_TILE_SIZE - eps)
        self.zoom_out_btn.setEnabled(zm.zoom() > MIN_TILE_SIZE + eps)

    # ------------------------------------------------------------------
    # Search flow

    def _on_search(self) -> None:
        query = self.query_input.text().strip()
        if not query:
            self.search_status_label.setText("Enter a search query first.")
            return

        # Stop any in-flight worker
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)

        self.canvas.clear()
        self._current_results = []
        self._update_result_controls()

        scope_media_type, scope_data = self._current_scope()
        scope = scope_data if scope_data else None
        field_data = self.field_combo.currentData()
        field      = None if field_data == "--all" else field_data
        limit_text = self.limit_combo.currentText()
        limit      = None if limit_text == "all" else int(limit_text)
        limit_per_movie = self.limit_per_movie_cb.isChecked()

        self.search_btn.setEnabled(False)
        self._progress.setRange(0, 0)  # indeterminate while searching
        self._progress.setValue(0)

        from tool import prefs as _prefs
        model_name = _prefs.get("model_frame_match", "clip-vit-base-patch32")

        self._worker = SearchWorker(
            query          = query,
            scope          = scope,
            field          = field,
            limit          = limit,
            limit_per_movie= limit_per_movie,
            project_path   = self.project_path,
            best_mode      = self.best_mode,
            model_name     = model_name,
            media_type     = scope_media_type,
        )
        self._worker.tile_ready.connect(self._on_tile_ready)
        self._worker.finished_signal.connect(self._on_search_done)
        self._worker.error.connect(self._on_search_error)
        self._worker.start()

    def _on_tile_ready(self, result: dict, pixmap) -> None:
        self._current_results.append(result)
        self.canvas.add_tile(result, pixmap)
        self._update_result_controls()

    def _on_search_done(self, count: int) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.search_btn.setEnabled(True)
        self._update_result_controls()
        self.search_status_label.setText(f"{count} result(s)")

    def _on_search_error(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.search_btn.setEnabled(True)
        self._update_best_button()
        preview = message.splitlines()[0][:120]
        self.search_status_label.setText(f"Error: {preview}")

    def _on_clear(self) -> None:
        if not self._current_results:
            return
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        self.canvas.clear()
        self._current_results = []
        self._selected_result = None
        self.info_block.clear()
        self.shotlist_btn.setEnabled(False)
        self._update_result_controls()
        self.search_status_label.setText("Enter a query and press Search")

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

        self._pdf_worker = PdfExportWorker(export_items, self.project_path, query, filename, subfolder)
        self._pdf_worker.finished_signal.connect(self._on_pdf_export_done)
        self._pdf_worker.error.connect(self._on_pdf_export_error)
        self._pdf_worker.start()

    def _on_pdf_export_done(self, out_path: str) -> None:
        import subprocess
        self.pdf_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        subprocess.Popen(["xdg-open", str(Path(out_path).parent)])

    def _on_pdf_export_error(self, message: str) -> None:
        self.pdf_btn.setEnabled(True)
        self.search_btn.setEnabled(True)

    def _on_save_video(self) -> None:
        """Generate a looping video mosaic (.mp4) from the current results."""
        if not self._current_results:
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
        _open_video_looping(out_path)

    def _on_video_error(self, message: str) -> None:
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self.video_btn.setEnabled(True)
        self.search_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Vocabulary panel

    def _on_field_changed(self) -> None:
        field = self.field_combo.currentData()
        self.vocab_table.clear()
        self.vocab_nav_combo.blockSignals(True)
        self.vocab_nav_combo.clear()
        self.vocab_nav_combo.setVisible(False)
        self.vocab_nav_combo.blockSignals(False)
        self._vocab_loading_timer.stop()
        self._vocab_loading_bar.stop()
        if not field:
            return

        self._start_vocabulary_load()

    def _start_vocabulary_load(self, prefix: Optional[str] = None) -> None:
        field = self.field_combo.currentData()
        if not field:
            return
        sort = self.vocab_sort_combo.currentData() or "count"

        scope_data = self.movie_combo.currentData()
        scope      = scope_data if scope_data else None

        self._vocab_request_id += 1
        request_id = self._vocab_request_id
        self.vocab_table.clear()
        self.vocab_rebuild_btn.setEnabled(False)
        self._vocab_loading_bar.start()
        self._vocab_loading_timer.start()

        self._vocab_worker = VocabularyWorker(
            field        = field,
            scope        = scope,
            project_path = self.project_path,
            media_type   = self.media_type,
            prefix       = prefix,
            sort         = sort,
            parent       = self,
        )
        self._vocab_worker.result_ready.connect(
            lambda result, rid=request_id: self._on_vocab_result(result, rid)
        )
        self._vocab_worker.error.connect(
            lambda message, rid=request_id: self._on_vocab_error(message, rid)
        )
        self._vocab_worker.finished.connect(self._vocab_worker.deleteLater)
        worker = self._vocab_worker
        QTimer.singleShot(
            0,
            lambda: self._start_queued_vocabulary_worker(request_id, worker),
        )

    def _start_queued_vocabulary_worker(
        self,
        request_id: int,
        worker: VocabularyWorker,
    ) -> None:
        """Start only the latest request after its cleared/loading UI can paint."""
        if request_id != self._vocab_request_id or worker is not self._vocab_worker:
            worker.deleteLater()
            return
        worker.start()

    def _on_vocab_result(self, result: dict, request_id: Optional[int] = None) -> None:
        if request_id is not None and request_id != self._vocab_request_id:
            return

        status = result.get("status")
        self.vocab_rebuild_btn.setEnabled(
            status in {"missing", "stale", "error"}
        )
        if status == "ready":
            items = result.get("items", [])
            self.vocab_nav_combo.blockSignals(True)
            self.vocab_nav_combo.clear()
            add_combo_all_item(self.vocab_nav_combo, user_data="--all")
            self.vocab_nav_combo.setItemText(0, "<A-Z>")
            for initial in result.get("initials", []):
                self.vocab_nav_combo.addItem(initial, initial)
            selected_prefix = result.get("selected_prefix", "--all")
            selected_index = self.vocab_nav_combo.findData(selected_prefix)
            self.vocab_nav_combo.setCurrentIndex(max(0, selected_index))
            self.vocab_nav_combo.setVisible(True)
            self.vocab_nav_combo.blockSignals(False)
            self.vocab_table.set_items(
                items,
                incremental=len(items) > VOCABULARY_RENDER_THRESHOLD,
            )
        elif status == "missing":
            self._stop_vocab_loading()
            self.vocab_nav_combo.setVisible(False)
            self.vocab_table.set_message("Vocabulary index is missing. Rebuild it to continue.")
        elif status == "stale":
            self._stop_vocab_loading()
            self.vocab_nav_combo.setVisible(False)
            self.vocab_table.set_message("Vocabulary index is stale. Rebuild it to continue.")
        elif status == "scope_unsupported":
            self._stop_vocab_loading()
            self.vocab_nav_combo.setVisible(False)
            self.vocab_table.set_message("Indexed vocabulary is available for --all titles.")
        else:
            self._stop_vocab_loading()
            self.vocab_nav_combo.setVisible(False)
            self.vocab_table.set_message("This field is not in the vocabulary index.")

    def _on_vocab_error(self, message: str, request_id: Optional[int] = None) -> None:
        if request_id is not None and request_id != self._vocab_request_id:
            return
        self._stop_vocab_loading()
        self.vocab_rebuild_btn.setEnabled(True)
        self.vocab_nav_combo.setVisible(False)
        preview = message.splitlines()[0][:120]
        self.vocab_table.set_message(f"Error loading vocabulary: {preview}")

    def _on_vocab_nav_changed(self, _index: int) -> None:
        prefix = self.vocab_nav_combo.currentData()
        if prefix:
            self._start_vocabulary_load(prefix)

    def _on_vocab_sort_changed(self, _index: int) -> None:
        from tool import prefs as _prefs
        _prefs.set(_VOCAB_SORT_PREF_KEY, self.vocab_sort_combo.currentData() or "count")
        prefix = self.vocab_nav_combo.currentData() or "--all"
        self._start_vocabulary_load(prefix)

    def _on_vocab_population_finished(self) -> None:
        self._stop_vocab_loading()

    def _stop_vocab_loading(self) -> None:
        self._vocab_loading_timer.stop()
        self._vocab_loading_bar.stop()

    def _on_vocab_rebuild(self) -> None:
        if self._vocab_rebuild_worker and self._vocab_rebuild_worker.isRunning():
            return

        self.vocab_rebuild_btn.setEnabled(False)
        self.vocab_rebuild_btn.setText("Rebuilding...")
        self.vocab_table.set_message("Rebuilding vocabulary index...")
        self._vocab_loading_bar.start()
        self._vocab_loading_timer.start()

        field = self.field_combo.currentData()
        family = "all" if field == "--all" else (
            "derived" if field in {"description", "text"} else "canonical"
        )
        self._vocab_rebuild_worker = VocabularyIndexWorker(self.media_type, family, self)
        self._vocab_rebuild_worker.finished_signal.connect(self._on_vocab_rebuild_done)
        self._vocab_rebuild_worker.error.connect(self._on_vocab_rebuild_error)
        self._vocab_rebuild_worker.start()

    def _reset_vocab_rebuild_ui(self, *, enable: bool) -> None:
        self._vocab_loading_timer.stop()
        self._vocab_loading_bar.stop()
        self.vocab_rebuild_btn.setText("Rebuild Vocabulary")
        self.vocab_rebuild_btn.setEnabled(enable)

    def _on_vocab_rebuild_done(self, _output: str) -> None:
        self._reset_vocab_rebuild_ui(enable=False)
        self._on_field_changed()

    def _on_vocab_rebuild_error(self, message: str) -> None:
        self._reset_vocab_rebuild_ui(enable=True)
        preview = message.splitlines()[0][:120]
        self.vocab_table.set_message(f"Rebuild failed: {preview}")

    def _on_vocab_item_clicked(self, value: str) -> None:
        if value:
            self.query_input.setText(value)


    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Intercept Home/End/PgUp/PgDn on combo boxes to override native
        QComboBox behaviour: Home/End step one title; PgUp/PgDn cycle field."""
        if event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Home, Qt.Key_End):
                # Navigate titles within the current movie_combo
                idx = self.movie_combo.currentIndex()
                if key == Qt.Key_Home:
                    if idx > 0:
                        self.movie_combo.setCurrentIndex(idx - 1)
                else:
                    if idx < self.movie_combo.count() - 1:
                        self.movie_combo.setCurrentIndex(idx + 1)
                return True
            if key in (Qt.Key_PageUp, Qt.Key_PageDown):
                if obj in (self.movie_combo, self.media_type_combo, self.field_combo, self.canvas, self.canvas.viewport()):
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

def run_visualizer(project_path: str, media_type: str = "movie") -> None:
    """Create the QApplication (if needed) and launch the visualizer window."""
    from visualizers.launcher import run_visualizer_window
    run_visualizer_window(
        "mosaic",
        lambda: MosaicVisualizer(project_path, media_type=media_type),
    )
