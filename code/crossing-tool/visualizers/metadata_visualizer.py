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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import save_window_geometry, restore_window_geometry

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QPixmap
from styles.theme import JumpScrollBar


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THUMB_W = 160   # max thumbnail width (aspect-ratio may give less)
THUMB_H = 60
CARD_H = 124           # fixed height: title + date + ~5 lines overview at 10pt
CARD_FONT_PT  = theme.BASE_PT + 1   # one step up from global base
CARD_SPACING = 6
COLUMN_MIN_W = 300
OVERVIEW_MAX_CHARS = 340


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_thumbnail(project_path: str, media_type: str, filename: str) -> Path | None:
    """Return the thumbnail path for *filename*, or None if not found."""
    stem = Path(filename).stem
    dirs_to_try = [media_type]
    if media_type == "movie":
        dirs_to_try.append("movies")  # backward-compat: existing projects store under movies/
    for mdir in dirs_to_try:
        thumb_dir = Path(project_path) / "media" / "thumbnails" / mdir
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


class _PlaceholderCard(QFrame):
    """Fixed-geometry skeleton card shown while metadata + thumbnails are loading."""

    _THUMB_BG = "#464646"
    _BAR_BG   = "#464646"
    _CARD_BG  = "#595959"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setFixedHeight(CARD_H)
        self.setStyleSheet(f"QFrame {{ background-color: {self._CARD_BG}; border: none; }}")

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        thumb_ph = QFrame()
        thumb_ph.setFixedSize(THUMB_W, CARD_H)
        thumb_ph.setStyleSheet(f"background-color: {self._THUMB_BG}; border: none;")
        row.addWidget(thumb_ph)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(6, 14, 6, 14)
        text_col.setSpacing(8)
        for bar_w in (140, 90, 200, 170, 110):
            bar = QFrame()
            bar.setFixedHeight(9)
            bar.setFixedWidth(bar_w)
            bar.setStyleSheet(
                f"background-color: {self._BAR_BG}; border: none; border-radius: 3px;"
            )
            text_col.addWidget(bar)
        text_col.addStretch(1)
        row.addLayout(text_col, 1)



_CARD_NORMAL = "QFrame { background-color: #666666; border: none; }"
_CARD_HOVER  = f"QFrame {{ background-color: {theme.ACCENT}; border: none; }}"
_CLI_PATH    = Path(__file__).parent.parent / "cli.py"


def _make_thumb_label(thumb_bytes: bytes) -> "QLabel":
    """Return a fixed-size thumbnail QLabel.  Width is derived from the image
    aspect ratio (minimum 40px); height is always CARD_H.
    Size is determined once at creation — no dynamic resizeEvent."""
    lbl = QLabel()
    lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    lbl.setStyleSheet("border: none; background-color: #464646;")
    w = THUMB_W  # fallback if no image
    if thumb_bytes:
        pix = QPixmap()
        if pix.loadFromData(thumb_bytes) and not pix.isNull() and pix.height() > 0:
            w = max(40, round(pix.width() * CARD_H / pix.height()))
            scaled = pix.scaled(w, CARD_H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            lbl.setPixmap(scaled)
    lbl.setFixedSize(w, CARD_H)
    return lbl


class _BaseCard(QFrame):
    """Common hover-highlight and click-to-open behaviour."""

    def __init__(self, filename: str, media_type: str, parent=None):
        super().__init__(parent)
        self._filename   = filename
        self._media_type = media_type
        self.setFrameShape(QFrame.NoFrame)
        self.setFixedHeight(CARD_H)
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
            from tool import prefs as _prefs
            from visualizers.shot_visualizer import open_at_shot
            open_at_shot(
                _prefs.get("path") or "",
                self._filename,
                self._media_type,
            )
        super().mousePressEvent(event)


class _MovieCard(_BaseCard):
    """Compact card for a single movie metadata record."""

    def __init__(self, record: dict, thumb_bytes: bytes, parent=None) -> None:
        super().__init__(record.get("filename", ""), "movie", parent)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        # Thumbnail — stable fixed-size label (size computed once from aspect ratio)
        thumb_label = _make_thumb_label(thumb_bytes)
        row.addWidget(thumb_label)

        # Text block
        text_col = QVBoxLayout()
        text_col.setContentsMargins(6, 6, 6, 6)
        text_col.setSpacing(2)

        title = record.get("title") or "(untitled)"
        title_label = QLabel(title)
        _f = theme.font_ui(bold=True); _f.setPointSize(CARD_FONT_PT)
        title_label.setFont(_f)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("background-color: transparent; border: none; color: #ffffff;")
        text_col.addWidget(title_label)

        year = str(record.get("year", "")) if record.get("year") else ""
        director = record.get("director", "")
        meta_parts = [p for p in (year, director) if p]
        if meta_parts:
            meta_label = QLabel(" \u00b7 ".join(meta_parts))
            meta_label.setObjectName("dim")
            _fm = theme.font_mono(); _fm.setPointSize(CARD_FONT_PT)
            meta_label.setFont(_fm)
            meta_label.setStyleSheet(f"background-color: transparent; border: none; color: {theme.TEXT_DIM};")
            meta_label.setWordWrap(True)
            text_col.addWidget(meta_label)

        overview = _truncate(record.get("overview", ""), OVERVIEW_MAX_CHARS)
        if overview:
            overview_label = QLabel(overview)
            overview_label.setObjectName("dim")
            _fo = theme.font_ui(); _fo.setPointSize(CARD_FONT_PT)
            overview_label.setFont(_fo)
            overview_label.setStyleSheet(f"background-color: transparent; border: none; color: {theme.TEXT_DIM};")
            overview_label.setWordWrap(True)
            overview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
            text_col.addWidget(overview_label)

        text_col.addStretch(1)
        row.addLayout(text_col, 1)


class _GameplayCard(_BaseCard):
    """Compact card for a single gameplay metadata record."""

    def __init__(self, record: dict, thumb_bytes: bytes, parent=None) -> None:
        super().__init__(record.get("filename", ""), "gameplay", parent)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        # Thumbnail — stable fixed-size label (size computed once from aspect ratio)
        thumb_label = _make_thumb_label(thumb_bytes)
        row.addWidget(thumb_label)

        # Text block
        text_col = QVBoxLayout()
        text_col.setContentsMargins(6, 6, 6, 6)
        text_col.setSpacing(2)

        title = record.get("title") or "(untitled)"
        title_label = QLabel(title)
        _f = theme.font_ui(bold=True); _f.setPointSize(CARD_FONT_PT)
        title_label.setFont(_f)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("background-color: transparent; border: none; color: #ffffff;")
        text_col.addWidget(title_label)

        game = record.get("game", "")
        if game:
            game_label = QLabel(game)
            game_label.setObjectName("dim")
            _fm = theme.font_mono(); _fm.setPointSize(CARD_FONT_PT)
            game_label.setFont(_fm)
            game_label.setStyleSheet(f"background-color: transparent; border: none; color: {theme.TEXT_DIM};")
            game_label.setWordWrap(True)
            text_col.addWidget(game_label)

        overview = _truncate(record.get("overview", ""), OVERVIEW_MAX_CHARS)
        if overview:
            overview_label = QLabel(overview)
            overview_label.setObjectName("dim")
            _fo = theme.font_ui(); _fo.setPointSize(CARD_FONT_PT)
            overview_label.setFont(_fo)
            overview_label.setStyleSheet(f"background-color: transparent; border: none; color: {theme.TEXT_DIM};")
            overview_label.setWordWrap(True)
            overview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
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
        self.setVerticalScrollBar(JumpScrollBar())

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
        # Pre-allocate skeleton placeholders so the column reaches its full height
        # immediately — subsequent fill_card calls do in-place swaps (no height change).
        self._placeholders: list = []
        if n <= 0:
            return
        container = self.widget()
        container.setUpdatesEnabled(False)
        try:
            for _ in range(n):
                ph = _PlaceholderCard()
                count = self._layout.count()
                self._layout.insertWidget(count - 1, ph)  # before trailing stretch
                self._placeholders.append(ph)
        finally:
            container.setUpdatesEnabled(True)

    def fill_card(self, index: int, card: QFrame) -> None:
        """Replace the placeholder at *index* with the real *card* in-place."""
        if index < len(self._placeholders):
            ph = self._placeholders[index]
            idx = self._layout.indexOf(ph)
            if idx >= 0:
                self._layout.insertWidget(idx, card)
                self._layout.removeWidget(ph)
                ph.deleteLater()
            self._placeholders[index] = card
        else:
            # Fallback: total_known was never called or count was wrong
            count = self._layout.count()
            self._layout.insertWidget(count - 1, card)

    def increment_progress(self) -> None:
        self._progress.setValue(self._progress.value() + 1)

    def finish_progress(self) -> None:
        self._progress.setValue(0)

    def add_card(self, card: QFrame) -> None:
        count = self._layout.count()
        self._layout.insertWidget(count - 1, card)

    def set_empty_message(self, message: str) -> None:
        lbl = QLabel(message)
        lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; background-color: transparent; border: none;")
        lbl.setAlignment(Qt.AlignCenter)
        self.add_card(lbl)

    def _remove_leftover_placeholders(self) -> None:
        """Remove any skeleton cards that never got a real card (e.g. load error)."""
        for ph in self._placeholders:
            if isinstance(ph, _PlaceholderCard):
                self._layout.removeWidget(ph)
                ph.deleteLater()
        self._placeholders = []


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
        restore_window_geometry(self, "window_metadata")
        self._start_loaders()

    def _start_loaders(self) -> None:
        # Per-column queues; drained one card per event-loop tick so the
        # UI repaints between every card insertion.
        self._movie_queue:    list[tuple[dict, bytes]] = []
        self._gameplay_queue: list[tuple[dict, bytes]] = []
        self._movie_draining    = False
        self._gameplay_draining = False
        self._movies_fill_idx   = 0
        self._gameplay_fill_idx = 0
        self._movies_total   = 0
        self._gameplay_total = 0

        self._movies_loader = _MetadataLoader(self._project_path, "movie", self)
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
            card = _MovieCard(record, thumb_bytes)
            self._movies_col.fill_card(self._movies_fill_idx, card)
            self._movies_fill_idx += 1
            self._movies_col.increment_progress()
            QTimer.singleShot(0, self._drain_movies)
        else:
            self._movie_draining = False

    def _on_movies_done(self, count: int) -> None:
        if count == 0:
            self._movies_col._remove_leftover_placeholders()
            self._movies_col.finish_progress()
            self._movies_col.set_empty_message("No movie metadata found.")
        else:
            QTimer.singleShot(0, self._finish_movies)

    def _finish_movies(self) -> None:
        if self._movie_queue or self._movie_draining:
            QTimer.singleShot(20, self._finish_movies)
        else:
            self._movies_col._remove_leftover_placeholders()
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
            card = _GameplayCard(record, thumb_bytes)
            self._gameplay_col.fill_card(self._gameplay_fill_idx, card)
            self._gameplay_fill_idx += 1
            self._gameplay_col.increment_progress()
            QTimer.singleShot(0, self._drain_gameplay)
        else:
            self._gameplay_draining = False

    def _on_gameplay_done(self, count: int) -> None:
        if count == 0:
            self._gameplay_col._remove_leftover_placeholders()
            self._gameplay_col.finish_progress()
            self._gameplay_col.set_empty_message("No gameplay metadata found.")
        else:
            QTimer.singleShot(0, self._finish_gameplay)

    def _finish_gameplay(self) -> None:
        if self._gameplay_queue or self._gameplay_draining:
            QTimer.singleShot(20, self._finish_gameplay)
        else:
            self._gameplay_col._remove_leftover_placeholders()
            self._gameplay_col.finish_progress()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Q, Qt.Key_W) and event.modifiers() & Qt.ControlModifier:
            self.close()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        save_window_geometry(self, "window_metadata")
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str) -> None:
    """Create the QApplication (if needed) and launch the metadata visualizer."""
    from visualizers._window_helpers import raise_existing_window
    if raise_existing_window("metadata"):
        return

    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    win = MetadataVisualizer(project_path)
    win.show()
    sys.exit(app.exec_())
