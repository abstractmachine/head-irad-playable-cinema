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

import os
import re
import sys
from pathlib import Path
from typing import Optional

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
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QImage, QPixmap, QWheelEvent

import cv2
import numpy as np

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


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

_DARK_BG    = "#121212"
_PANEL_BG   = "#1e1e1e"
_CTRL_BG    = "#232323"
_BORDER     = "#444"
_TEXT       = "#dddddd"
_TEXT_DIM   = "#888888"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_frame_pixmap(video_path: Path, frame_index: int) -> Optional[QPixmap]:
    """Extract a single video frame and return as QPixmap. Returns None on failure."""
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
        parent=None,
    ):
        super().__init__(parent)
        self.query           = query
        self.scope           = scope
        self.field           = field
        self.limit           = limit
        self.limit_per_movie = limit_per_movie
        self.project_path    = project_path
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
# Individual tile widget
# ---------------------------------------------------------------------------

class TileWidget(QFrame):
    """A single mosaic tile: scaled video frame + short caption + hover tooltip."""

    _PLACEHOLDER_BG = "#2a2a2a"

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

        self.setFrameShape(QFrame.Box)
        self.setFrameShadow(QFrame.Plain)
        self.setLineWidth(1)
        self.setStyleSheet(f"TileWidget {{ border: 1px solid {_BORDER}; background: {_PANEL_BG}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.setStyleSheet(f"background: {self._PLACEHOLDER_BG}; border: none;")
        layout.addWidget(self._img_label)

        caption_text = _short_title(result.get("movie_title", "") or result.get("movie_id", ""))
        self._cap_label = QLabel(caption_text)
        self._cap_label.setAlignment(Qt.AlignCenter)
        self._cap_label.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 9px; border: none;")
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
        """Return (img_w, img_h) for the given tile_size (height)."""
        img_h = tile_size
        img_w = int(tile_size * 16 / 9)
        return img_w, img_h

    def _render(self, tile_size: int) -> None:
        img_w, img_h = self._tile_dimensions(tile_size)
        self.setFixedSize(img_w + 4, img_h + 22)
        self._img_label.setFixedSize(img_w, img_h)

        if self.original_pixmap is not None and not self.original_pixmap.isNull():
            scaled = self.original_pixmap.scaled(
                img_w, img_h,
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            # Centre-crop to exact tile dimensions
            if scaled.width() > img_w or scaled.height() > img_h:
                x = (scaled.width()  - img_w) // 2
                y = (scaled.height() - img_h) // 2
                scaled = scaled.copy(x, y, img_w, img_h)
            self._img_label.setPixmap(scaled)
            self._img_label.setText("")
        else:
            self._img_label.setPixmap(QPixmap())
            self._img_label.setText("⚠")
            self._img_label.setStyleSheet(
                f"background: {self._PLACEHOLDER_BG}; color: #555; font-size: 24px; border: none;"
            )

    def resize_tile(self, tile_size: int) -> None:
        """Re-render at a new tile_size (called when user zooms)."""
        self._render(tile_size)


# ---------------------------------------------------------------------------
# Mosaic canvas — scrollable grid of tiles
# ---------------------------------------------------------------------------

class MosaicCanvas(QScrollArea):
    """Scrollable viewport containing a grid of TileWidget tiles.

    Ctrl + scroll wheel  → zoom in / out
    Plain scroll wheel   → vertical scroll (default)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setStyleSheet(f"QScrollArea {{ border: none; background: {_DARK_BG}; }}")

        self._tile_size: int = DEFAULT_TILE_SIZE
        self._tiles: list[TileWidget] = []
        self._col_count: int = 0

        self._container = QWidget()
        self._container.setStyleSheet(f"background: {_DARK_BG};")
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(4)
        self._grid.setContentsMargins(8, 8, 8, 8)
        self.setWidget(self._container)

    # ------------------------------------------------------------------
    # Public API

    @property
    def tile_count(self) -> int:
        return len(self._tiles)

    def clear(self) -> None:
        for tile in self._tiles:
            self._grid.removeWidget(tile)
            tile.deleteLater()
        self._tiles.clear()
        self._col_count = 0

    def add_tile(self, result: dict, pixmap) -> None:
        tile = TileWidget(result, pixmap, self._tile_size)
        self._tiles.append(tile)
        self._place_last_tile()

    # ------------------------------------------------------------------
    # Layout

    def _columns_for_width(self) -> int:
        tile_w = int(self._tile_size * 16 / 9) + 4 + self._grid.horizontalSpacing()
        vp_w   = max(self.viewport().width(), tile_w + 1)
        return max(1, vp_w // tile_w)

    def _place_last_tile(self) -> None:
        """Fast-path: only append the very last tile if column count is stable."""
        cols = self._columns_for_width()
        if cols != self._col_count:
            self._col_count = cols
            self._full_reflow()
            return
        idx = len(self._tiles) - 1
        self._grid.addWidget(self._tiles[-1], idx // cols, idx % cols)

    def _full_reflow(self) -> None:
        cols = self._col_count or 1
        for i, tile in enumerate(self._tiles):
            self._grid.addWidget(tile, i // cols, i % cols)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        new_cols = self._columns_for_width()
        if new_cols != self._col_count:
            self._col_count = new_cols
            self._full_reflow()

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
        self._col_count = 0
        cols = self._columns_for_width()
        self._col_count = cols
        self._full_reflow()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

_STYLESHEET = f"""
QMainWindow, QWidget      {{ background: {_PANEL_BG}; color: {_TEXT}; }}
QLabel                    {{ color: {_TEXT}; }}
QComboBox, QLineEdit      {{
    background: #2d2d2d; color: {_TEXT};
    border: 1px solid #555; padding: 4px 6px;
    border-radius: 3px;
}}
QComboBox::drop-down      {{ border: none; }}
QPushButton               {{
    background: #2d2d2d; color: {_TEXT};
    border: 1px solid #555; padding: 5px 12px;
    border-radius: 3px;
}}
QPushButton:hover         {{ background: #3a3a3a; }}
QPushButton:pressed       {{ background: #484848; }}
QPushButton:disabled      {{ color: #555; border-color: #3a3a3a; }}
QGroupBox                 {{
    border: 1px solid {_BORDER}; border-radius: 4px;
    margin-top: 10px; color: #999;
    font-size: 10px;
}}
QGroupBox::title          {{
    subcontrol-origin: margin; left: 8px; padding: 0 4px;
}}
QCheckBox                 {{ color: #ccc; spacing: 6px; }}
QScrollBar:vertical       {{ background: #1a1a1a; width: 10px; }}
QScrollBar::handle:vertical {{ background: #444; border-radius: 4px; }}
QStatusBar                {{ background: #1a1a1a; color: {_TEXT_DIM}; }}
"""

_CTRL_PANEL_WIDTH = 270


class MosaicVisualizer(QMainWindow):
    """Interactive mosaic visualizer window."""

    def __init__(self, project_path: str):
        super().__init__()
        self.project_path = project_path
        self._worker: Optional[SearchWorker] = None
        self._vocab_worker: Optional[VocabularyWorker] = None

        self.setWindowTitle("Crossing — Mosaic Visualizer")
        self.resize(1440, 900)
        self.setStyleSheet(_STYLESHEET)

        # Central splitter: left = canvas, right = controls
        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        self.canvas = MosaicCanvas()
        splitter.addWidget(self.canvas)

        ctrl = self._build_control_panel()
        splitter.addWidget(ctrl)
        splitter.setSizes([1150, _CTRL_PANEL_WIDTH])

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready — enter a query and press Search.")

        self._populate_movies()

    # ------------------------------------------------------------------
    # Control panel

    def _build_control_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(_CTRL_PANEL_WIDTH)
        panel.setStyleSheet(f"QWidget {{ background: {_CTRL_BG}; }}")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        # Movie scope
        scope_group = QGroupBox("Movie Scope")
        scope_layout = QVBoxLayout(scope_group)
        scope_layout.setContentsMargins(8, 12, 8, 8)
        self.movie_combo = QComboBox()
        self.movie_combo.addItem("--all")
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
        query_layout.addWidget(self.query_input)
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._on_search)
        query_layout.addWidget(self.search_btn)
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
        layout.addWidget(opt_group)

        # Vocabulary
        vocab_group = QGroupBox("Vocabulary")
        vocab_layout = QVBoxLayout(vocab_group)
        vocab_layout.setContentsMargins(4, 12, 4, 4)
        vocab_layout.setSpacing(0)
        self.vocab_list = QListWidget()
        self.vocab_list.setStyleSheet(f"""
            QListWidget {{
                background: #1a1a1a;
                border: none;
                color: {_TEXT};
                font-size: 11px;
            }}
            QListWidget::item {{
                padding: 3px 8px;
                border-bottom: 1px solid #252525;
            }}
            QListWidget::item:hover     {{ background: #2a2a2a; }}
            QListWidget::item:selected  {{ background: #333; }}
        """)
        self.vocab_list.itemClicked.connect(self._on_vocab_item_clicked)
        vocab_layout.addWidget(self.vocab_list)
        layout.addWidget(vocab_group, 1)  # stretch=1 fills remaining space
        return panel

    def _populate_movies(self) -> None:
        try:
            from services.metadata import get_metadata
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
    # Search flow

    def _on_search(self) -> None:
        query = self.query_input.text().strip()
        if not query:
            self.status.showMessage("Enter a search query first.")
            return

        # Stop any in-flight worker
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)

        self.canvas.clear()

        scope_text      = self.movie_combo.currentText()
        scope           = None if scope_text == "--all" else scope_text
        field_text      = self.field_combo.currentText()
        field           = None if field_text == "--all" else field_text
        limit_text      = self.limit_combo.currentText()
        limit           = None if limit_text == "all" else int(limit_text)
        limit_per_movie = self.limit_per_movie_cb.isChecked()

        self.search_btn.setEnabled(False)
        self.status.showMessage(f"Searching for '{query}'…")

        self._worker = SearchWorker(
            query          = query,
            scope          = scope,
            field          = field,
            limit          = limit,
            limit_per_movie= limit_per_movie,
            project_path   = self.project_path,
        )
        self._worker.tile_ready.connect(self._on_tile_ready)
        self._worker.finished_signal.connect(self._on_search_done)
        self._worker.error.connect(self._on_search_error)
        self._worker.start()

    def _on_tile_ready(self, result: dict, pixmap) -> None:
        self.canvas.add_tile(result, pixmap)
        self.status.showMessage(f"Loading… {self.canvas.tile_count} tile(s)")

    def _on_search_done(self, count: int) -> None:
        self.search_btn.setEnabled(True)
        if count == 0:
            self.status.showMessage("No results found.")
        else:
            self.status.showMessage(
                f"{count} result(s)  —  Ctrl + scroll to zoom,  scroll to pan"
            )

    def _on_search_error(self, message: str) -> None:
        self.search_btn.setEnabled(True)
        preview = message.splitlines()[0][:120]
        self.status.showMessage(f"Error: {preview}")

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

        scope_text = self.movie_combo.currentText()
        scope      = None if scope_text == "--all" else scope_text

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


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str) -> None:
    """Create the QApplication (if needed) and launch the visualizer window."""
    app = QApplication.instance() or QApplication(sys.argv)
    win = MosaicVisualizer(project_path)
    win.show()
    sys.exit(app.exec_())
