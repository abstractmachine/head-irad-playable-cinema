#!/usr/bin/env python3
"""Metadata Visualizer — canonical two-pane browser for Movies and Gameplay.

Launched via:
    crossing visualizer metadata

Layout:
  LEFT  — tabbed thumbnail browser (Movies / Gameplay)
  RIGHT — single Info inspector

The browser owns selection only. The inspector reflects the selected record.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import GripSplitter, JumpScrollBar, restore_window_geometry, save_window_geometry
from tool import prefs as _prefs
from tool.shortcuts import VisualizerWindow
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.metadata_block import MetadataBlock
from visualizers.components.thumbnail_cell import ThumbnailCell
from visualizers.components.thumbnail_loader import ThumbnailLoader

from PyQt5.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QImage, QWheelEvent
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)
_THUMB_SIZE = 120
_THUMB_GAP = theme.SECTION_GAP
_INSPECTOR_MIN_W = 280
_ZOOM_MIN = 0.75
_ZOOM_MAX = 1.75
_ZOOM_STEP = 0.10
_ZOOM_DEFAULT = 1.00


def _zoom_key(media_type: str) -> str:
    return f"metadata_browser_zoom_{media_type}"


def _wrap_anywhere(text: str) -> str:
    return "\u200b".join(text)


def _browser_title_font(zoom: float) -> QFont:
    font = theme.font_ui()
    font.setPointSize(max(6, round(theme.BASE_PT * zoom)))
    font.setWeight(theme.WEIGHT_UI)
    return font

_INFO_ROWS = [
    "title",
    "filename",
    "original_filename",
    "media_type",
    "year",
    "director",
    "game",
    "duration",
    "tagline",
    "overview",
    "media_id",
    "tmdb",
    "imdb",
]


def _resolve_thumbnail(project_path: str, media_type: str, filename: str) -> Path | None:
    stem = Path(filename).stem
    dirs_to_try = [media_type]
    if media_type == "movie":
        dirs_to_try.append("movies")
    for mdir in dirs_to_try:
        thumb_dir = Path(project_path) / "media" / "thumbnails" / mdir
        for name in (stem + ".jpg", stem.replace(" ", "-") + ".jpg"):
            path = thumb_dir / name
            if path.exists():
                return path
    return None


def _format_value(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (list, tuple)):
        text = ", ".join(_format_value(item) for item in value if item not in (None, "")) or "—"
        return _wrap_anywhere(text)
    if isinstance(value, dict):
        try:
            return _wrap_anywhere(json.dumps(value, ensure_ascii=True, sort_keys=True))
        except Exception:
            return _wrap_anywhere(str(value))
    return _wrap_anywhere(str(value))


class _MetadataBrowserPage(QWidget):
    selectionChanged = pyqtSignal(object)

    def __init__(self, project_path: str, media_type: str, heading: str, parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._media_type = media_type
        self._heading = heading
        self._records: list[dict] = []
        self._cells: list[ThumbnailCell] = []
        self._selected_index = -1
        self._grid_cols = 1
        self._loader: ThumbnailLoader | None = None
        self._zoom = float(_prefs.get(_zoom_key(media_type), _ZOOM_DEFAULT) or _ZOOM_DEFAULT)
        self._item_by_index: list[_BrowserItem] = []

        self.setFocusPolicy(Qt.StrongFocus)
        self.setStyleSheet(f"QWidget {{ background: {theme.CANVAS_BG}; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setStyleSheet(f"QScrollArea {{ background: {theme.CANVAS_BG}; border: none; }}")
        self._scroll.setVerticalScrollBar(JumpScrollBar())
        self._scroll.viewport().installEventFilter(self)

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._grid_layout.setSpacing(_THUMB_GAP)
        self._grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self._empty_label = QLabel(f"No {heading.lower()} metadata found.")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; background: transparent; font-size: {theme.BASE_PT}pt;"
        )
        self._empty_label.setWordWrap(True)

        self._grid_layout.addWidget(self._empty_label, 0, 0)
        self._scroll.setWidget(self._grid_widget)
        outer.addWidget(self._scroll)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._relayout_cells)

    def set_records(self, records: list[dict]) -> None:
        self._stop_loader()
        self._records = list(records)
        self._selected_index = 0 if self._records else -1
        self._rebuild_grid(select_first=True)

    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float, persist: bool = True) -> None:
        zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, zoom))
        if abs(zoom - self._zoom) < 1e-6:
            return
        self._zoom = zoom
        if persist:
            _prefs.set(_zoom_key(self._media_type), self._zoom)
        for item in self._item_by_index:
            item.set_zoom(self._zoom)
        self._grid_layout.invalidate()
        self._grid_widget.adjustSize()

    def _change_zoom(self, delta: float) -> None:
        self.set_zoom(self._zoom + delta)

    def current_record(self) -> dict | None:
        if 0 <= self._selected_index < len(self._records):
            return self._records[self._selected_index]
        return None

    def _thumbnail_path_for(self, record: dict) -> Path | None:
        return _resolve_thumbnail(
            self._project_path,
            self._media_type,
            record.get("filename", ""),
        )

    def _clear_grid(self) -> None:
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self._empty_label:
                widget.deleteLater()
        self._cells = []
        self._item_by_index = []

    def _cols(self) -> int:
        viewport_width = self._scroll.viewport().width() or 200
        return max(1, (viewport_width - _THUMB_GAP) // (_THUMB_SIZE + _THUMB_GAP))

    def _rebuild_grid(self, select_first: bool = False) -> None:
        self._clear_grid()
        if not self._records:
            self._grid_layout.addWidget(self._empty_label, 0, 0)
            self.selectionChanged.emit(None)
            return

        cols = self._cols()
        self._grid_cols = cols
        active_index = self._selected_index
        if active_index < 0 and select_first:
            active_index = 0
        if active_index >= len(self._records):
            active_index = len(self._records) - 1
        self._selected_index = active_index

        for index, record in enumerate(self._records):
            title = record.get("title") or Path(record.get("filename", "")).stem or "(untitled)"
            subtitle = record.get("year") or record.get("game") or record.get("director") or ""
            tooltip = title if not subtitle else f"{title} — {subtitle}"
            item = _BrowserItem(index=index, title=title, tooltip=tooltip, zoom=self._zoom)
            item.clicked.connect(self._on_cell_clicked)
            item.doubleClicked.connect(self._on_cell_double_clicked)
            item.set_selected(index == self._selected_index)
            self._cells.append(item.thumbnail())
            self._item_by_index.append(item)
            self._grid_layout.addWidget(item, index // cols, index % cols)

        self._start_loader()
        self._apply_zoom_to_items()
        self._emit_current_selection()

    def _apply_zoom_to_items(self) -> None:
        for item in self._item_by_index:
            item.set_zoom(self._zoom)

    def _start_loader(self) -> None:
        if not self._records:
            return
        self._loader = ThumbnailLoader(
            self._records,
            _THUMB_SIZE,
            path_for=self._thumbnail_path_for,
            parent=self,
        )
        self._loader.thumbReady.connect(self._on_thumb_ready)
        self._loader.start()

    def _stop_loader(self) -> None:
        if self._loader is None:
            return
        try:
            self._loader.thumbReady.disconnect(self._on_thumb_ready)
        except (TypeError, RuntimeError):
            pass
        if self._loader.isRunning():
            self._loader.cancel()
            self._loader.wait(300)
        self._loader = None

    def _on_thumb_ready(self, index: int, qimg: QImage) -> None:
        if 0 <= index < len(self._item_by_index):
            self._item_by_index[index].set_image(qimg)

    def _set_selected_index(self, index: int, emit: bool = True) -> None:
        if not self._records:
            self._selected_index = -1
            if emit:
                self.selectionChanged.emit(None)
            return

        index = max(0, min(index, len(self._records) - 1))
        if index == self._selected_index and emit:
            self._emit_current_selection()
            return

        if 0 <= self._selected_index < len(self._item_by_index):
            self._item_by_index[self._selected_index].set_selected(False)

        self._selected_index = index

        if 0 <= self._selected_index < len(self._item_by_index):
            self._item_by_index[self._selected_index].set_selected(True)
            self._scroll.ensureWidgetVisible(self._item_by_index[self._selected_index])

        if emit:
            self._emit_current_selection()

    def _emit_current_selection(self) -> None:
        self.selectionChanged.emit(self.current_record())

    def _on_cell_clicked(self, index: int) -> None:
        self.setFocus()
        self._set_selected_index(index, emit=True)

    def _on_cell_double_clicked(self, index: int) -> None:
        self._on_cell_clicked(index)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._scroll.viewport() and event.type() == QEvent.Wheel:
            wheel = event  # type: ignore[assignment]
            if wheel.modifiers() & Qt.ControlModifier:
                delta = wheel.angleDelta().y()
                if delta > 0:
                    self._change_zoom(_ZOOM_STEP)
                elif delta < 0:
                    self._change_zoom(-_ZOOM_STEP)
                wheel.accept()
                return True
        return super().eventFilter(obj, event)

    def _relayout_cells(self) -> None:
        if not self._item_by_index:
            return
        cols = self._cols()
        self._grid_cols = cols
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self._empty_label:
                widget.setParent(None)
        for index, item_widget in enumerate(self._item_by_index):
            self._grid_layout.addWidget(item_widget, index // cols, index % cols)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._resize_timer.start(0)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        mod = event.modifiers()
        if mod & Qt.ControlModifier:
            if key in (Qt.Key_Plus, Qt.Key_Equal):
                self._change_zoom(_ZOOM_STEP)
                return
            if key in (Qt.Key_Minus, Qt.Key_Underscore):
                self._change_zoom(-_ZOOM_STEP)
                return
            if key == Qt.Key_0:
                self.set_zoom(_ZOOM_DEFAULT)
                return
        if key == Qt.Key_Home:
            self._set_selected_index(0)
            return
        if key == Qt.Key_End:
            self._set_selected_index(len(self._records) - 1)
            return

        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            if not self._records:
                return
            cols = max(1, self._grid_cols)
            delta = {
                Qt.Key_Left: -1,
                Qt.Key_Right: 1,
                Qt.Key_Up: -cols,
                Qt.Key_Down: cols,
            }[key]
            self._set_selected_index(self._selected_index + delta)
            return

        super().keyPressEvent(event)


class _BrowserItem(QWidget):
    clicked = pyqtSignal(int)
    doubleClicked = pyqtSignal(int)

    def __init__(self, index: int, title: str, tooltip: str = "", zoom: float = 1.0, parent=None) -> None:
        super().__init__(parent)
        self._index = index
        self._raw_title = title
        self._zoom = zoom
        self._thumb = ThumbnailCell(index=index, size=_THUMB_SIZE, tooltip=tooltip, parent=self)
        self._thumb.drag_path = ""
        self._thumb.drag_meta = {}
        self._thumb.clicked.connect(lambda _idx: self.clicked.emit(self._index))
        self._thumb.doubleClicked.connect(lambda _idx: self.doubleClicked.emit(self._index))

        self._title = QLabel(self)
        self._title.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self._title.setWordWrap(True)
        self._title.setTextInteractionFlags(Qt.NoTextInteraction)
        self._title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._title.setStyleSheet(
            f"background: transparent; color: {theme.TEXT};"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        outer.addWidget(self._thumb, 0, Qt.AlignHCenter)
        outer.addWidget(self._title, 0)

        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.set_zoom(zoom)

    def thumbnail(self) -> ThumbnailCell:
        return self._thumb

    def set_image(self, qimg: QImage) -> None:
        self._thumb.set_image(qimg)

    def set_selected(self, selected: bool) -> None:
        self._thumb.set_selected(selected)

    def set_zoom(self, zoom: float) -> None:
        self._zoom = zoom
        self._title.setFont(_browser_title_font(zoom))
        self._title.setText(_wrap_anywhere(self._raw_title))
        self._title.setFixedWidth(_THUMB_SIZE)
        self._title.setMinimumHeight(self._title.sizeHint().height())
        self.adjustSize()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._index)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.doubleClicked.emit(self._index)
        super().mouseDoubleClickEvent(event)


class MetadataVisualizer(VisualizerWindow):
    def __init__(self, project_path: str) -> None:
        super().__init__()
        self._project_path = project_path
        self._inspector_hidden = False
        self._saved_splitter_sizes: list[int] = []
        self._selected_records: dict[str, dict | None] = {"movie": None, "gameplay": None}

        self.setWindowTitle("Crossing — Metadata Visualizer")

        root = QWidget()
        root.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self.setCentralWidget(root)

        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._splitter = GripSplitter(Qt.Horizontal)
        layout.addWidget(self._splitter)

        self._browser_stack = QStackedWidget()
        self._browser_stack.setContentsMargins(0, 0, 0, 0)

        self._movie_page = _MetadataBrowserPage(project_path, "movie", "Movies")
        self._gameplay_page = _MetadataBrowserPage(project_path, "gameplay", "Gameplay")
        self._movie_page.selectionChanged.connect(lambda rec: self._on_page_selection_changed("movie", rec))
        self._gameplay_page.selectionChanged.connect(lambda rec: self._on_page_selection_changed("gameplay", rec))

        self._browser_stack.addWidget(self._movie_page)
        self._browser_stack.addWidget(self._gameplay_page)

        self._inspector_shell = self._build_inspector()

        self._splitter.addWidget(self._browser_stack)
        self._splitter.addWidget(self._inspector_shell)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)

        self.setMinimumSize(980, 640)
        restore_window_geometry(self, "window_metadata")
        QTimer.singleShot(0, self._fit_splitter_width)

        self._load_records()

    def closeEvent(self, event) -> None:
        save_window_geometry(self, "window_metadata")
        super().closeEvent(event)

    def _load_records(self) -> None:
        from data.metadata import get_metadata

        try:
            movie_records = get_metadata(self._project_path, media_type="movie")
        except Exception:
            movie_records = []

        try:
            gameplay_records = get_metadata(self._project_path, media_type="gameplay")
        except Exception:
            gameplay_records = []

        self._movie_page.set_records(movie_records)
        self._gameplay_page.set_records(gameplay_records)
        self._sync_inspector_to_current_tab()

    def _build_inspector(self) -> QWidget:
        outer = QWidget()
        outer.setStyleSheet(f"background: {theme.CANVAS_BG};")
        outer.setMinimumWidth(_INSPECTOR_MIN_W)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        tabs = QTabBar()
        tabs.setExpanding(False)
        tabs.setUsesScrollButtons(False)
        tabs.setDrawBase(False)
        tabs.setFocusPolicy(Qt.NoFocus)
        tabs.setFocusPolicy(Qt.NoFocus)
        tabs.setStyleSheet(theme.tab_strip_stylesheet())
        self._source_tabs = tabs

        tabs.addTab(" Movies ")
        tabs.addTab(" Gameplay ")

        tabs.currentChanged.connect(self._on_source_tab_changed)

        outer_layout.addWidget(tabs)

        pane = QWidget()
        pane.setStyleSheet(f"background: {theme.TAB_BG};")
        pane_layout = QVBoxLayout(pane)
        pane_layout.setContentsMargins(2, 2, 2, 2)
        pane_layout.setSpacing(2)

        self._info_section = CollapsibleSection("Info", pref_key="metadata_section_info")
        self._info_section.setStyleSheet(f"background: {theme.TAB_BG};")
        info_wrap = QWidget()
        info_layout = QVBoxLayout(info_wrap)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)

        self._info_block = MetadataBlock(_INFO_ROWS)
        info_layout.addWidget(self._info_block)
        self._info_section.add_widget(info_wrap)

        pane_layout.addWidget(self._info_section)
        outer_layout.addWidget(pane)
        outer_layout.addStretch(1)
        return outer

    def _fit_splitter_width(self) -> None:
        inspector_w = max(_INSPECTOR_MIN_W, self._inspector_shell.sizeHint().width())
        total_w = max(self.width(), 980)
        browser_w = max(1, total_w - inspector_w)
        self._splitter.setSizes([browser_w, inspector_w])
        self._saved_splitter_sizes = [browser_w, inspector_w]

    def _toggle_inspector(self) -> None:
        if self._inspector_hidden:
            self._inspector_shell.show()
            if self._saved_splitter_sizes:
                self._splitter.setSizes(self._saved_splitter_sizes)
            else:
                self._fit_splitter_width()
            self._inspector_hidden = False
            return

        self._saved_splitter_sizes = self._splitter.sizes()
        self._inspector_shell.hide()
        self._splitter.setSizes([max(1, self.width() - 1), 0])
        self._inspector_hidden = True

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _on_page_selection_changed(self, media_type: str, record: object) -> None:
        self._selected_records[media_type] = record if isinstance(record, dict) else None
        source_tabs = getattr(self, "_source_tabs", None)
        if source_tabs is None:
            return
        current_page = "movie" if source_tabs.currentIndex() == 0 else "gameplay"
        if current_page == media_type:
            self._show_record(record if isinstance(record, dict) else None)

    def _sync_inspector_to_current_tab(self) -> None:
        source_tabs = getattr(self, "_source_tabs", None)
        if source_tabs is None:
            return
        current_index = source_tabs.currentIndex()
        current_page = "movie" if current_index == 0 else "gameplay"
        self._browser_stack.setCurrentIndex(current_index)
        self._show_record(self._selected_records.get(current_page))

    def _on_source_tab_changed(self, index: int) -> None:
        self._browser_stack.setCurrentIndex(index)
        self._sync_inspector_to_current_tab()

    def _show_record(self, record: dict | None) -> None:
        if record is None:
            for key in _INFO_ROWS:
                self._info_block.set(key, "—")
            return

        values = {
            "title": record.get("title"),
            "filename": record.get("filename"),
            "original_filename": record.get("original_filename"),
            "media_type": record.get("media_type"),
            "year": record.get("year"),
            "director": record.get("director"),
            "game": record.get("game"),
            "duration": record.get("duration"),
            "tagline": record.get("tagline"),
            "overview": record.get("overview"),
            "media_id": record.get("media_id"),
            "tmdb": record.get("tmdb"),
            "imdb": record.get("imdb"),
        }
        for key in _INFO_ROWS:
            value = _format_value(values.get(key))
            if key == "overview" and value != "—":
                value = value[:260].rstrip() + ("…" if len(value) > 260 else "")
            self._info_block.set(key, value)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        mod = event.modifiers()
        if key == Qt.Key_Escape:
            self.close()
            return
        if key in (Qt.Key_Q, Qt.Key_W) and mod & Qt.ControlModifier:
            self.close()
            return
        if key == Qt.Key_Tab and not (mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)):
            if mod & Qt.ShiftModifier:
                self._toggle_fullscreen()
            else:
                self._toggle_inspector()
            return
        if key == Qt.Key_Backtab:
            self._toggle_fullscreen()
            return
        super().keyPressEvent(event)


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
