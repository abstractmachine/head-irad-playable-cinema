#!/usr/bin/env python3
"""Metadata Visualizer — scrollable card-based browser for Movies and Gameplay.

Launched via:
    crossing visualizer metadata

Layout:
  LEFT  — Movies column (scrollable card list)
  RIGHT — Gameplay column (scrollable card list)

Each card shows a thumbnail, title, and key metadata fields.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QStyleOptionSlider


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THUMB_W = 90
THUMB_H = 60
CARD_SPACING = 6
COLUMN_MIN_W = 300
OVERVIEW_MAX_CHARS = 180


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_thumbnail(project_path: str, media_type: str, filename: str) -> Path | None:
    """Return the thumbnail path for *filename*, or None if not found."""
    stem = Path(filename).stem
    thumb_dir = Path(project_path) / "media" / "thumbnails" / media_type
    for name in (stem + ".jpg", stem.replace(" ", "-") + ".jpg"):
        p = thumb_dir / name
        if p.exists():
            return p
    return None


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


# ---------------------------------------------------------------------------
# Card widgets
# ---------------------------------------------------------------------------

_CARD_NORMAL = "QFrame { background-color: #666666; border: none; }"
_CARD_HOVER  = f"QFrame {{ background-color: {theme.ACCENT}; border: none; }}"
_CLI_PATH    = Path(__file__).parent.parent / "cli.py"


class _BaseCard(QFrame):
    """Common hover-highlight and click-to-open behaviour."""

    def __init__(self, filename: str, media_type: str, parent=None):
        super().__init__(parent)
        self._filename   = filename
        self._media_type = media_type
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(_CARD_NORMAL)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, event) -> None:
        self.setStyleSheet(_CARD_HOVER)
        for child in self.findChildren(QLabel, "dim"):
            child.setStyleSheet(f"background-color: transparent; border: none; color: #333333;")
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.setStyleSheet(_CARD_NORMAL)
        for child in self.findChildren(QLabel, "dim"):
            child.setStyleSheet(f"background-color: transparent; border: none; color: {theme.TEXT_DIM};")
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._filename:
            subprocess.Popen([
                sys.executable, str(_CLI_PATH),
                "visualizer", "shotlist",
                "--media", self._media_type,
                "--filename", self._filename,
            ])
        super().mousePressEvent(event)


class _MovieCard(_BaseCard):
    """Compact card for a single movie metadata record."""

    def __init__(self, record: dict, thumb_bytes: bytes, parent=None) -> None:
        super().__init__(record.get("filename", ""), "movies", parent)

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(8)

        # Thumbnail
        thumb_label = QLabel()
        thumb_label.setFixedSize(THUMB_W, THUMB_H)
        thumb_label.setAlignment(Qt.AlignCenter)
        thumb_label.setStyleSheet(f"background-color: {theme.CANVAS_BG}; border: none;")
        if thumb_bytes:
            pix = QPixmap()
            pix.loadFromData(thumb_bytes)
            pix = pix.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            thumb_label.setPixmap(pix)
        row.addWidget(thumb_label, 0, Qt.AlignTop)

        # Text block
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        title = record.get("title") or "(untitled)"
        title_label = QLabel(title)
        title_label.setFont(theme.font_ui(bold=True))
        title_label.setWordWrap(True)
        title_label.setStyleSheet("background-color: transparent; border: none; color: #ffffff;")
        text_col.addWidget(title_label)

        year = str(record.get("year", "")) if record.get("year") else ""
        director = record.get("director", "")
        meta_parts = [p for p in (year, director) if p]
        if meta_parts:
            meta_label = QLabel(" · ".join(meta_parts))
            meta_label.setObjectName("dim")
            meta_label.setFont(theme.font_mono())
            meta_label.setStyleSheet(f"background-color: transparent; border: none; color: {theme.TEXT_DIM};")
            meta_label.setWordWrap(True)
            text_col.addWidget(meta_label)

        overview = _truncate(record.get("overview", ""), OVERVIEW_MAX_CHARS)
        if overview:
            overview_label = QLabel(overview)
            overview_label.setObjectName("dim")
            overview_label.setFont(theme.font_ui())
            overview_label.setStyleSheet(f"background-color: transparent; border: none; color: {theme.TEXT_DIM};")
            overview_label.setWordWrap(True)
            overview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            text_col.addWidget(overview_label)

        text_col.addStretch(1)
        row.addLayout(text_col, 1)


class _GameplayCard(_BaseCard):
    """Compact card for a single gameplay metadata record."""

    def __init__(self, record: dict, thumb_bytes: bytes, parent=None) -> None:
        super().__init__(record.get("filename", ""), "gameplay", parent)

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(8)

        # Thumbnail
        thumb_label = QLabel()
        thumb_label.setFixedSize(THUMB_W, THUMB_H)
        thumb_label.setAlignment(Qt.AlignCenter)
        thumb_label.setStyleSheet(f"background-color: {theme.CANVAS_BG}; border: none;")
        if thumb_bytes:
            pix = QPixmap()
            pix.loadFromData(thumb_bytes)
            pix = pix.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            thumb_label.setPixmap(pix)
        row.addWidget(thumb_label, 0, Qt.AlignTop)

        # Text block
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        title = record.get("title") or "(untitled)"
        title_label = QLabel(title)
        title_label.setFont(theme.font_ui(bold=True))
        title_label.setWordWrap(True)
        title_label.setStyleSheet("background-color: transparent; border: none; color: #ffffff;")
        text_col.addWidget(title_label)

        game = record.get("game", "")
        if game:
            game_label = QLabel(game)
            game_label.setObjectName("dim")
            game_label.setFont(theme.font_mono())
            game_label.setStyleSheet(f"background-color: transparent; border: none; color: {theme.TEXT_DIM};")
            game_label.setWordWrap(True)
            text_col.addWidget(game_label)

        overview = _truncate(record.get("overview", ""), OVERVIEW_MAX_CHARS)
        if overview:
            overview_label = QLabel(overview)
            overview_label.setObjectName("dim")
            overview_label.setFont(theme.font_ui())
            overview_label.setStyleSheet(f"background-color: transparent; border: none; color: {theme.TEXT_DIM};")
            overview_label.setWordWrap(True)
            overview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            text_col.addWidget(overview_label)

        text_col.addStretch(1)
        row.addLayout(text_col, 1)


# ---------------------------------------------------------------------------
# Background loader
# ---------------------------------------------------------------------------

class _MetadataLoader(QThread):
    """Loads metadata + thumbnails for one media type and emits them one at a time.

    Thumbnail file I/O is done here so the main thread only has to do fast
    in-memory QPixmap.loadFromData() work between event-loop ticks.
    """

    total_known    = pyqtSignal(int)         # emitted once before any records
    record_ready   = pyqtSignal(dict, bytes) # (record dict, raw JPEG bytes or b"")
    finished_loading = pyqtSignal(int)       # total count when done

    def __init__(self, project_path: str, media_type: str, parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._media_type = media_type

    def run(self) -> None:
        try:
            from data.metadata import get_metadata
            records = get_metadata(self._project_path, media_type=self._media_type)
            self.total_known.emit(len(records))
            for record in records:
                thumb_bytes = b""
                thumb_path = _resolve_thumbnail(
                    self._project_path, self._media_type, record.get("filename", "")
                )
                if thumb_path:
                    try:
                        thumb_bytes = thumb_path.read_bytes()
                    except OSError:
                        pass
                self.record_ready.emit(record, thumb_bytes)
            self.finished_loading.emit(len(records))
        except Exception:
            self.total_known.emit(0)
            self.finished_loading.emit(0)


# ---------------------------------------------------------------------------
# Custom scrollbar — click-to-jump + full-bar hover highlight
# ---------------------------------------------------------------------------

class _JumpScrollBar(QScrollBar):
    """Vertical scrollbar with two UX improvements over the default:

    1. Hovering *anywhere* on the bar immediately highlights the handle fuchsia
       (not just when the cursor is directly on the handle thumb).
    2. Clicking in the track (not on the handle) jumps the viewport to that
       position instantly instead of doing a page-step.
    """

    _STYLE_IDLE = (
        "QScrollBar:vertical { background: transparent; width: 16px; }"
        "QScrollBar::handle:vertical {"
        "    background: transparent;"
        f"   border-left: 2px solid {theme.ACCENT};"
        "    border-radius: 0; min-height: 20px; }"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
    )
    _STYLE_HOVER = (
        "QScrollBar:vertical { background: transparent; width: 16px; }"
        "QScrollBar::handle:vertical {"
        f"   background: {theme.ACCENT};"
        f"   border-left: 2px solid {theme.ACCENT};"
        "    border-radius: 0; min-height: 20px; }"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
    )

    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Vertical, parent)
        self.setStyleSheet(self._STYLE_IDLE)
        self._drag_active = False

    def _groove(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarGroove, self
        )
        return groove, opt.upsideDown

    def enterEvent(self, event) -> None:
        self.setStyleSheet(self._STYLE_HOVER)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._drag_active:
            self.setStyleSheet(self._STYLE_IDLE)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle_rect = self.style().subControlRect(
                QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarSlider, self
            )
            if not handle_rect.contains(event.pos()):
                # Click in track: jump to that position and begin drag
                groove = self.style().subControlRect(
                    QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarGroove, self
                )
                pos = event.y() - groove.y()
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), pos, groove.height(),
                    opt.upsideDown,
                )
                self.setValue(value)
                self._drag_active = True
                self.grabMouse()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_active:
            from PyQt5.QtGui import QCursor
            global_pos = QCursor.pos()
            local = self.mapFromGlobal(global_pos)
            groove, upside_down = self._groove()
            pos = local.y() - groove.y()
            value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), pos, groove.height(), upside_down
            )
            self.setValue(value)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_active and event.button() == Qt.LeftButton:
            self._drag_active = False
            self.releaseMouse()
            from PyQt5.QtGui import QCursor
            if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                self.setStyleSheet(self._STYLE_IDLE)
            return
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Column widget — scrollable list of cards
# ---------------------------------------------------------------------------

class _CardColumn(QScrollArea):
    """A labeled, vertically-scrollable column of cards."""

    def __init__(self, heading: str, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setMinimumWidth(COLUMN_MIN_W)
        self.setStyleSheet(
            f"QScrollArea {{ border: none; background-color: {theme.BG}; }}"
        )
        self.setVerticalScrollBar(_JumpScrollBar())

        container = QWidget()
        container.setStyleSheet(f"background-color: {theme.BG};")
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(CARD_SPACING)

        heading_label = QLabel(heading)
        heading_label.setFont(theme.font_ui(bold=True))
        heading_label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12pt; "
            "padding-bottom: 4px; background-color: transparent;"
        )
        self._layout.addWidget(heading_label)

        # 1px separator that doubles as a loading progress bar.
        # During loading: fuchsia chunk grows left-to-right over the UI_BORDER base.
        # When done: the bar is hidden and the heading's bottom spacing is enough.
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
        self._layout.addWidget(self._progress)
        self._layout.addSpacing(4)

        self._layout.addStretch(1)
        self.setWidget(container)

    def set_total(self, n: int) -> None:
        self._progress.setRange(0, max(n, 1))

    def increment_progress(self) -> None:
        self._progress.setValue(self._progress.value() + 1)

    def finish_progress(self) -> None:
        self._progress.setValue(0)

    def add_card(self, card: QFrame) -> None:
        # Insert before the trailing stretch
        count = self._layout.count()
        self._layout.insertWidget(count - 1, card)

    def set_empty_message(self, message: str) -> None:
        lbl = QLabel(message)
        lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; background-color: transparent; border: none;")
        lbl.setAlignment(Qt.AlignCenter)
        self.add_card(lbl)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MetadataVisualizer(QMainWindow):

    def __init__(self, project_path: str) -> None:
        super().__init__()
        self.setWindowTitle("Crossing — Metadata")
        self._project_path = project_path

        root = QWidget()
        self.setCentralWidget(root)

        outer = QHBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        self._movies_col = _CardColumn("Movies")
        self._gameplay_col = _CardColumn("Gameplay")

        outer.addWidget(self._movies_col, 1)
        outer.addWidget(self._gameplay_col, 1)

        self.resize(900, 700)
        self._start_loaders()

    def _start_loaders(self) -> None:
        # Per-column queues; drained one card per event-loop tick so the
        # UI repaints between every card insertion.
        self._movie_queue:    list[tuple[dict, bytes]] = []
        self._gameplay_queue: list[tuple[dict, bytes]] = []
        self._movie_draining    = False
        self._gameplay_draining = False
        self._movies_total   = 0
        self._gameplay_total = 0

        self._movies_loader = _MetadataLoader(self._project_path, "movies", self)
        self._movies_loader.total_known.connect(self._movies_col.set_total)
        self._movies_loader.record_ready.connect(self._on_movie_record)
        self._movies_loader.finished_loading.connect(self._on_movies_done)
        self._movies_loader.start()

        self._gameplay_loader = _MetadataLoader(self._project_path, "gameplay", self)
        self._gameplay_loader.total_known.connect(self._gameplay_col.set_total)
        self._gameplay_loader.record_ready.connect(self._on_gameplay_record)
        self._gameplay_loader.finished_loading.connect(self._on_gameplay_done)
        self._gameplay_loader.start()

    # -- movies -------------------------------------------------------------

    def _on_movie_record(self, record: dict, thumb_bytes: bytes) -> None:
        self._movie_queue.append((record, thumb_bytes))
        if not self._movie_draining:
            self._movie_draining = True
            QTimer.singleShot(0, self._drain_movies)

    def _drain_movies(self) -> None:
        if self._movie_queue:
            record, thumb_bytes = self._movie_queue.pop(0)
            self._movies_col.add_card(_MovieCard(record, thumb_bytes))
            self._movies_col.increment_progress()
            QTimer.singleShot(0, self._drain_movies)
        else:
            self._movie_draining = False

    def _on_movies_done(self, count: int) -> None:
        if count == 0:
            self._movies_col.finish_progress()
            self._movies_col.set_empty_message("No movie metadata found.")
        else:
            # Drain any remaining queued cards then hide the bar
            QTimer.singleShot(0, self._finish_movies)

    def _finish_movies(self) -> None:
        if self._movie_queue or self._movie_draining:
            QTimer.singleShot(20, self._finish_movies)
        else:
            self._movies_col.finish_progress()

    # -- gameplay -----------------------------------------------------------

    def _on_gameplay_record(self, record: dict, thumb_bytes: bytes) -> None:
        self._gameplay_queue.append((record, thumb_bytes))
        if not self._gameplay_draining:
            self._gameplay_draining = True
            QTimer.singleShot(0, self._drain_gameplay)

    def _drain_gameplay(self) -> None:
        if self._gameplay_queue:
            record, thumb_bytes = self._gameplay_queue.pop(0)
            self._gameplay_col.add_card(_GameplayCard(record, thumb_bytes))
            self._gameplay_col.increment_progress()
            QTimer.singleShot(0, self._drain_gameplay)
        else:
            self._gameplay_draining = False

    def _on_gameplay_done(self, count: int) -> None:
        if count == 0:
            self._gameplay_col.finish_progress()
            self._gameplay_col.set_empty_message("No gameplay metadata found.")
        else:
            QTimer.singleShot(0, self._finish_gameplay)

    def _finish_gameplay(self) -> None:
        if self._gameplay_queue or self._gameplay_draining:
            QTimer.singleShot(20, self._finish_gameplay)
        else:
            self._gameplay_col.finish_progress()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Q, Qt.Key_W) and event.modifiers() & Qt.ControlModifier:
            self.close()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str) -> None:
    """Create the QApplication (if needed) and launch the metadata visualizer."""
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    win = MetadataVisualizer(project_path)
    win.show()
    sys.exit(app.exec_())
