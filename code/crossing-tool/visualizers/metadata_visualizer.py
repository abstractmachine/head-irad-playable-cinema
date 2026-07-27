#!/usr/bin/env python3
"""Metadata Visualizer — canonical two-pane browser for Movies and Gameplay.

Launched via:
    crossing visualizer metadata

Layout:
  LEFT  — tabbed thumbnail browser (Movies / Gameplay)
  RIGHT — single Info inspector

The browser owns selection only. The inspector reflects the selected record.
"""

# TODO:
# A small number of metadata records (~8 titles) produce a one-row height
# overshoot in MetadataBlock. The layout is otherwise correct.
#
# Do not "fix" this with timers, adjustSize(), processEvents(), or
# custom text measurement. Previous experiments made the behavior worse.
#
# If revisited, investigate QLabel's effective wrapping width rather than
# replacing Qt's text layout.

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import JumpScrollBar
from tool import prefs as _prefs
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.inspector import Inspector
from visualizers.components.metadata_block import MetadataBlock
from visualizers.components.thumbnail_manager import ThumbnailManager
from visualizers.components.flow_widget import FlowWidget
from visualizers.components.selection_manager import SelectionManager
from visualizers.shot_visualizer import open_at_shot

from PyQt5.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap, QWheelEvent
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QGridLayout,
    QScrollArea,
    QStackedWidget,
    QTabBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
_THUMB_SIZE = 120
_THUMB_GAP = theme.SECTION_GAP
_INSPECTOR_MIN_W = 280
_ZOOM_MIN = 0.60
_ZOOM_MAX = 7.00
_ZOOM_STEP = 0.10
_ZOOM_DEFAULT = 1.00
_THUMB_LOAD_SIZE = int(round(_THUMB_SIZE * _ZOOM_MAX))


def _zoom_key(media_type: str) -> str:
    return f"metadata_browser_zoom_{media_type}"


def _wrap_anywhere(text: str) -> str:
    return "\u200b".join(text)


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
    openRequested = pyqtSignal(object)

    def __init__(self, project_path: str, media_type: str, heading: str, parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._media_type = media_type
        self._heading = heading
        self._records: list[dict] = []
        self._selected_index = -1
        self._grid_cols = 1
        # ThumbnailManager owns the loader lifecycle and forwards images.
        self._thumb_manager: ThumbnailManager = ThumbnailManager(self)
        self._zoom = float(_prefs.get(_zoom_key(media_type), _ZOOM_DEFAULT) or _ZOOM_DEFAULT)
        self._item_by_index: list[_BrowserItem] = []
        # SelectionManager owns selection index and side-effects.
        self._selection_manager = SelectionManager(self)

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

        self._grid_widget = FlowWidget()
        self._grid_widget.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self._empty_label = QLabel(f"No {heading.lower()} metadata found.")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; background: transparent; font-size: {theme.BASE_PT}pt;"
        )
        self._empty_label.setWordWrap(True)

        self._grid_widget.set_empty_label(self._empty_label)
        self._scroll.setWidget(self._grid_widget)
        outer.addWidget(self._scroll)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._relayout_cells)

    def request_reflow(self) -> None:
        self._resize_timer.start(0)

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
        # Immediately reflow the grid so zoom changes jump to the new scale
        # instead of animating or stepping gradually. If the direct call
        # fails for any reason, fall back to the debounced request_reflow().
        try:
            self._grid_widget._do_flow_layout()
            self._grid_cols = max(1, self._grid_widget.first_row_count())
        except Exception:
            self.request_reflow()

    def _change_zoom(self, delta: float) -> None:
        self.set_zoom(self._zoom + delta)

    def current_record(self) -> dict | None:
        if 0 <= self._selected_index < len(self._records):
            return self._records[self._selected_index]
        return None

    def current_thumbnail_path(self) -> Path | None:
        record = self.current_record()
        if record is None:
            return None
        return self._thumbnail_path_for(record)

    def _thumbnail_path_for(self, record: dict) -> Path | None:
        return _resolve_thumbnail(
            self._project_path,
            self._media_type,
            record.get("filename", ""),
        )

    def _clear_grid(self) -> None:
        self._grid_widget.clear_cells()
        self._item_by_index = []

    def create_item_for_record(self, index: int, record: dict) -> _BrowserItem:
        """Create and return a `_BrowserItem` for `record` at `index`.

        This extracts the existing inline construction logic from
        `_rebuild_grid()` so the browser can delegate item creation.
        Behavior is intentionally identical to the original code.
        """
        title = record.get("title") or Path(record.get("filename", "")).stem or "(untitled)"
        subtitle = record.get("year") or record.get("game") or record.get("director") or ""
        tooltip = title if not subtitle else f"{title} — {subtitle}"
        item = _BrowserItem(index=index, tooltip=tooltip, zoom=self._zoom, media_type=self._media_type)
        return item

    def _rebuild_grid(self, select_first: bool = False) -> None:
        self._clear_grid()
        if not self._records:
            self.selectionChanged.emit(None)
            return

        active_index = self._selected_index
        if active_index < 0 and select_first:
            active_index = 0
        if active_index >= len(self._records):
            active_index = len(self._records) - 1
        # Delegate selection index ownership to SelectionManager.
        try:
            self._selection_manager.set_selected_index(active_index, emit=False)
        except Exception:
            # Fallback: mirror on the page for compatibility
            self._selected_index = active_index

        for index, record in enumerate(self._records):
            item = self.create_item_for_record(index, record)
            item.clicked.connect(self._on_cell_clicked)
            item.doubleClicked.connect(self._on_cell_double_clicked)
            item.set_selected(index == self._selected_index)
            self._item_by_index.append(item)

        self._grid_widget.set_cells(self._item_by_index)
        self._start_loader()
        self._apply_zoom_to_items()
        self._emit_current_selection()

    def _apply_zoom_to_items(self) -> None:
        for item in self._item_by_index:
            item.set_zoom(self._zoom)
        self.request_reflow()

    def _start_loader(self) -> None:
        if not self._records:
            return

        def _apply_cb(index: int, qimg: QImage) -> None:
            try:
                if 0 <= index < len(self._item_by_index):
                    self._item_by_index[index].set_image(qimg)
                    self.request_reflow()
            except Exception:
                pass

        self._thumb_manager.start(
            self._records,
            _THUMB_LOAD_SIZE,
            path_for=self._thumbnail_path_for,
            apply_callback=_apply_cb,
        )

    def _stop_loader(self) -> None:
        try:
            self._thumb_manager.stop()
        except Exception:
            pass

    # Note: thumbnail delivery is now handled by ThumbnailManager; images
    # are forwarded to the grid via the apply_callback provided in
    # `_start_loader`.

    def _set_selected_index(self, index: int, emit: bool = True) -> None:
        try:
            self._selection_manager.set_selected_index(index, emit=emit)
        except Exception:
            # Fallback to previous inline behavior on error
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
        try:
            self._selection_manager.emit_current_selection()
        except Exception:
            self.selectionChanged.emit(self.current_record())

    def _on_cell_clicked(self, index: int) -> None:
        try:
            self._selection_manager.on_cell_clicked(index)
        except Exception:
            self.setFocus()
            self._set_selected_index(index, emit=True)

    def _on_cell_double_clicked(self, index: int) -> None:
        try:
            self._selection_manager.on_cell_double_clicked(index)
        except Exception:
            self._on_cell_clicked(index)
            self.openRequested.emit(self.current_record())

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._scroll.viewport() and event.type() == QEvent.Wheel:
            wheel = event  # type: ignore[assignment]
            if wheel.modifiers() & Qt.ControlModifier:
                # Map the wheel delta into discrete zoom steps and apply a
                # direct jump to the resulting zoom so the change is immediate.
                # angleDelta is usually a multiple of 120 per notch.
                delta = wheel.angleDelta().y()
                try:
                    notches = int(delta / 120)
                except Exception:
                    notches = 1 if delta > 0 else -1 if delta < 0 else 0
                if notches != 0:
                    self.set_zoom(self._zoom + notches * _ZOOM_STEP)
                else:
                    # Fallback to a single step when delta is small.
                    self._change_zoom(_ZOOM_STEP if delta > 0 else -_ZOOM_STEP)
                wheel.accept()
                return True
        if obj is self._scroll.viewport() and event.type() == QEvent.Resize:
            self.request_reflow()
        return super().eventFilter(obj, event)

    def _relayout_cells(self) -> None:
        if not self._item_by_index:
            return
        self._grid_widget.request_reflow()
        self._grid_cols = max(1, self._grid_widget.first_row_count())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.request_reflow()

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
            try:
                from visualizers.components.selection_manager import SelectionManager

                mapping = {
                    Qt.Key_Left: SelectionManager.Direction.LEFT,
                    Qt.Key_Right: SelectionManager.Direction.RIGHT,
                    Qt.Key_Up: SelectionManager.Direction.UP,
                    Qt.Key_Down: SelectionManager.Direction.DOWN,
                }
                direction = mapping.get(key)
                if direction is not None:
                    try:
                        self._selection_manager.move(direction)
                    except Exception:
                        # Fallback to previous inline arithmetic
                        cols = max(1, self._grid_cols)
                        delta = {
                            Qt.Key_Left: -1,
                            Qt.Key_Right: 1,
                            Qt.Key_Up: -cols,
                            Qt.Key_Down: cols,
                        }[key]
                        self._set_selected_index(self._selected_index + delta)
            except Exception:
                # If mapping/import fails, preserve legacy behavior
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

    def __init__(self, index: int, tooltip: str = "", zoom: float = 1.0, media_type: str = "movie", parent=None) -> None:
        super().__init__(parent)
        self._index = index
        self._zoom = zoom
        self._media_type = media_type
        self._qimg: QImage | None = None
        self._thumb = QLabel(self)
        self._thumb.setAlignment(Qt.AlignCenter)
        self._thumb.setCursor(Qt.PointingHandCursor)
        self._thumb.setFocusPolicy(Qt.NoFocus)
        self._thumb.setStyleSheet("background: transparent;")
        if tooltip:
            self.setToolTip(tooltip)
            self._thumb.setToolTip(tooltip)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._thumb, 0, Qt.AlignCenter)

        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        # Let the parent `_BrowserItem` receive mouse events so clicks
        # and double-clicks are handled exactly once. Making the label
        # transparent to mouse events avoids duplicate handlers firing
        # when both the label and parent receive the same event.
        try:
            self._thumb.setAttribute(Qt.WA_TransparentForMouseEvents)
        except Exception:
            # Fallback: bind events explicitly if attribute isn't available.
            self._thumb.mousePressEvent = self.mousePressEvent  # type: ignore[assignment]
            self._thumb.mouseDoubleClickEvent = self.mouseDoubleClickEvent  # type: ignore[assignment]
        self.set_zoom(zoom)

    def _frame_size(self) -> tuple[int, int]:
        target = max(48, int(round(_THUMB_SIZE * self._zoom)))
        # If we have a loaded image, respect its natural aspect ratio so
        # the browser highlights and layout match the media's shape.
        if self._qimg is not None and not self._qimg.isNull():
            try:
                iw = float(self._qimg.width())
                ih = float(self._qimg.height())
                ratio = iw / ih if ih > 0 else 1.0
            except Exception:
                ratio = 1.0
            frame_h = target
            frame_w = max(32, int(round(frame_h * ratio)))
            return frame_w, frame_h

        # Fallback behaviour when no image is available yet: preserve the
        # previous movie shortcut (approx 2.35/1 -> 0.67 width ratio) and
        # square cells for other media types so layout is stable until the
        # thumbnail loads.
        if self._media_type == "movie":
            return max(32, int(round(target * 0.67))), target
        return target, target

    def set_image(self, qimg: QImage) -> None:
        self._qimg = qimg
        self._update_thumbnail()

    def set_selected(self, selected: bool) -> None:
        if self.property("selected") != selected:
            self.setProperty("selected", selected)
            self._apply_style()

    def set_zoom(self, zoom: float) -> None:
        self._zoom = zoom
        self._update_thumbnail()

    def _apply_style(self) -> None:
        border = theme.ACCENT if self.property("selected") else "transparent"
        self.setStyleSheet(
            f"background: {theme.CANVAS_BG}; border: 2px solid {border};"
        )

    def _update_thumbnail(self) -> None:
        target = max(48, int(round(_THUMB_SIZE * self._zoom)))
        frame_w, frame_h = self._frame_size()
        if self._qimg is None or self._qimg.isNull():
            self._thumb.setPixmap(QPixmap())
            self._thumb.setFixedSize(frame_w, frame_h)
            self.setFixedSize(frame_w + 4, frame_h + 4)
            self._apply_style()
            return

        scaled = self._qimg.scaled(frame_w, frame_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._thumb.setPixmap(QPixmap.fromImage(scaled))
        self._thumb.setFixedSize(frame_w, frame_h)
        self.setFixedSize(frame_w + 4, frame_h + 4)
        self._apply_style()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._index)
            try:
                event.accept()
            except Exception:
                pass
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.doubleClicked.emit(self._index)
            try:
                event.accept()
            except Exception:
                pass
            return
        super().mouseDoubleClickEvent(event)


class MetadataVisualizer(WindowVisualizer):
    def __init__(self, project_path: str) -> None:
        # Initialize attributes required by create_browser()/create_inspector()
        # before the WindowVisualizer constructor runs (it calls those hooks).
        self._project_path = project_path
        # Let the window shell persist geometry under this pref key
        super().__init__(pref_key="window_metadata")
        self._inspector_hidden = False
        self._inspector_auto_collapsed = False
        self._saved_splitter_sizes: list[int] = []
        self._last_visible_splitter_sizes: list[int] = []
        self._selected_records: dict[str, dict | None] = {"movie": None, "gameplay": None}
        self._current_thumbnail_path: Path | None = None
        self._inspector_collapse_w = 0
        self._inspector_restore_w = 0
        self._inspector_scrollbar_visible = False
        self._inspector_scrollbar_extent = 0
        # Counter for click-driven metadata dumps
        self._metadata_click_count = 0

        self.setWindowTitle("Crossing — Metadata Visualizer")

        # Browser and inspector are created via WindowVisualizer hooks.
        # `create_browser` and `create_inspector` will be called from
        # the WindowVisualizer constructor; configure the splitter
        # behavior here after the shell exists.
        self.setMinimumSize(980, 640)
        # Tweak splitter behavior set by the shell
        try:
            self._splitter.setProperty("snap_right_pane_on_drag", False)
            self._splitter.splitterMoved.connect(self._on_splitter_moved)
        except Exception:
            pass

        # Populate browser/inspector content and load records
        self._load_records()

    def closeEvent(self, event) -> None:
        # Geometry persistence handled by WindowVisualizer; just propagate.
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
        # Schedule a post-populate debug dump so we capture layout after
        # the inspector has been populated with data.
        try:
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(0, lambda: self._debug_dump("post_populate"))
        except Exception:
            pass

        # If two filenames are provided via environment variables, run a
        # comparative instrumentation pass selecting each film and printing
        # detailed layout diagnostics. Supports either METADATA_GOOD/ BAD or
        # the legacy METADATA_NONOVERFLOW_FILE / METADATA_OVERFLOW_FILE.
        try:
            import os

            good_file = os.environ.get("METADATA_GOOD_FILE") or os.environ.get("METADATA_NONOVERFLOW_FILE")
            bad_file = os.environ.get("METADATA_BAD_FILE") or os.environ.get("METADATA_OVERFLOW_FILE")
            if good_file and bad_file:
                def _find_record_index(page, target):
                    try:
                        for i, r in enumerate(page._records):
                            fn = str(r.get("filename") or "")
                            title = str(r.get("title") or "")
                            if fn == target or Path(fn).name == target or Path(fn).stem == target or title == target:
                                return i
                    except Exception:
                        pass
                    return None

                def _run_cases():
                    # Ensure movie tab selected for both cases
                    try:
                        if hasattr(self, "_source_tabs") and self._source_tabs is not None:
                            self._source_tabs.setCurrentIndex(0)
                    except Exception:
                        pass

                    # GOOD case
                    print("\n=== GOOD CASE ===")
                    idx_good = _find_record_index(self._movie_page, good_file)
                    if idx_good is None:
                        print(f"Good record not found: {good_file}")
                    else:
                        try:
                            self._movie_page._set_selected_index(idx_good, emit=True)
                        except Exception:
                            pass

                        def _capture_good():
                            try:
                                print(f"--- SELECTED: {good_file} ---")
                                # Reuse the verbose dump
                                try:
                                    self._debug_dump("GOOD CASE")
                                except Exception:
                                    pass
                                # Capture structured state and print tailored details
                                good_state = self._capture_inspector_state()
                                self._print_case_state(good_state, "GOOD CASE")
                                # Save for later comparison
                                self._last_good_state = good_state
                            except Exception:
                                import traceback

                                traceback.print_exc()

                        QTimer.singleShot(300, _capture_good)

                    # BAD case (run after a delay so the UI updates from GOOD)
                    def _run_bad():
                        print("\n=== BAD CASE ===")
                        idx_bad = _find_record_index(self._movie_page, bad_file)
                        if idx_bad is None:
                            print(f"Bad record not found: {bad_file}")
                            return
                        try:
                            self._movie_page._set_selected_index(idx_bad, emit=True)
                        except Exception:
                            pass

                        def _capture_bad():
                            try:
                                print(f"--- SELECTED: {bad_file} ---")
                                try:
                                    self._debug_dump("BAD CASE")
                                except Exception:
                                    pass
                                bad_state = self._capture_inspector_state()
                                self._print_case_state(bad_state, "BAD CASE")
                                # If we have the good state, compare and summarize
                                try:
                                    good_state = getattr(self, "_last_good_state", None)
                                    if good_state is not None:
                                        self._compare_and_summarize(good_state, bad_state)
                                except Exception:
                                    import traceback

                                    traceback.print_exc()
                            except Exception:
                                import traceback

                                traceback.print_exc()

                        QTimer.singleShot(300, _capture_bad)

                    # Schedule BAD after GOOD has been captured
                    QTimer.singleShot(1000, _run_bad)

                QTimer.singleShot(200, _run_cases)
        except Exception:
            pass

    def _build_inspector(self) -> QWidget:
        inspector = Inspector(self)
        inspector.connect_scrollbar_range_changed(self._on_inspector_scrollbar_range_changed)

        # Configure content size
        inspector.set_minimum_width(_INSPECTOR_MIN_W)

        # Top source tabs (non-collapsible)
        tabs = QTabBar()
        tabs.setExpanding(False)
        tabs.setUsesScrollButtons(False)
        tabs.setDrawBase(False)
        tabs.setFocusPolicy(Qt.NoFocus)
        tabs.setStyleSheet(theme.tab_strip_stylesheet())
        self._source_tabs = tabs

        tabs.addTab(" Movies ")
        tabs.addTab(" Gameplay ")

        tabs.currentChanged.connect(self._on_source_tab_changed)

        inspector.panel().add_widget(tabs, alignment=Qt.AlignTop)

        # Thumbnail section
        thumbnail_wrap = QWidget()
        thumbnail_layout = QVBoxLayout(thumbnail_wrap)
        thumbnail_layout.setContentsMargins(0, 0, 0, 0)
        thumbnail_layout.setSpacing(0)
        self._thumbnail_label = QLabel("No thumbnail")
        self._thumbnail_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        # Let the TabPanel paint the canonical pane background; keep the
        # thumbnail label itself transparent so the panel shows through.
        # Ensure no internal margins/padding so the image sits flush with
        # the section body edges.
        self._thumbnail_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; background: transparent; margin: 0px; padding: 0px; border: none;"
        )
        self._thumbnail_label.setMinimumHeight(140)
        self._thumbnail_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        thumbnail_layout.addWidget(self._thumbnail_label)
        self._thumbnail_section = inspector.panel().add_section("Thumbnail", thumbnail_wrap, pref_key="metadata_section_thumbnail")

        # Info section
        info_wrap = QWidget()
        info_layout = QVBoxLayout(info_wrap)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)

        self._info_block = MetadataBlock(_INFO_ROWS)
        info_layout.addWidget(self._info_block)
        self._info_section = inspector.panel().add_section("Info", info_wrap, pref_key="metadata_section_info")

        # Tools section
        tools_wrap = QWidget()
        tools_layout = QVBoxLayout(tools_wrap)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        tools_layout.setSpacing(theme.INSPECTOR_GAP)
        tools_grid = QGridLayout()
        tools_grid.setContentsMargins(0, 0, 0, 0)
        tools_grid.setSpacing(theme.INSPECTOR_GAP)
        tools_grid.setColumnStretch(0, 1)
        tools_grid.setColumnStretch(1, 1)

        action_style = theme.action_button_stylesheet()
        # Ensure inspector action buttons use the same UI font weight as
        # CollapsibleSection titles so button text feels visually consistent.
        action_style = (
            action_style
            + f"QPushButton {{ font-family: '{theme.FAMILY_UI}'; font-weight: {theme.WEIGHT_UI}; }}"
        )

        self._zoom_in_btn = QPushButton("Zoom +")
        self._zoom_in_btn.setFocusPolicy(Qt.NoFocus)
        self._zoom_in_btn.setStyleSheet(action_style)
        self._zoom_in_btn.clicked.connect(self._zoom_in_current_page)
        tools_grid.addWidget(self._zoom_in_btn, 0, 0)

        self._zoom_out_btn = QPushButton("Zoom -")
        self._zoom_out_btn.setFocusPolicy(Qt.NoFocus)
        self._zoom_out_btn.setStyleSheet(action_style)
        self._zoom_out_btn.clicked.connect(self._zoom_out_current_page)
        tools_grid.addWidget(self._zoom_out_btn, 0, 1)

        self._shotlist_btn = QPushButton("Shotlist")
        self._shotlist_btn.setFocusPolicy(Qt.NoFocus)
        self._shotlist_btn.setStyleSheet(action_style)
        self._shotlist_btn.setToolTip("Open the selected movie or gameplay entry in Shotlist")
        self._shotlist_btn.clicked.connect(self._open_selected_in_shotlist)
        tools_grid.addWidget(self._shotlist_btn, 1, 0, 1, 2)

        tools_layout.addLayout(tools_grid)
        self._tools_section = inspector.panel().add_section("Tools", tools_wrap, pref_key="metadata_section_tools")

        # Temporary debug instrumentation: schedule a startup dump of
        # inspector/widget geometry and layout metrics. This is read-only
        # instrumentation only — no behaviour or style changes.
        try:
            from PyQt5.QtCore import QTimer

            def _delayed_startup_dump():
                try:
                    self._debug_dump("startup")
                except Exception:
                    import traceback

                    traceback.print_exc()

            QTimer.singleShot(0, _delayed_startup_dump)
        except Exception:
            pass

        return inspector

    # ----------------------- Debug instrumentation -----------------------
    def _widget_geom_summary(self, w):
        try:
            geom = w.geometry()
            size_hint = w.sizeHint()
            min_size_hint = w.minimumSizeHint()
            return {
                "class": w.__class__.__name__,
                "objectName": w.objectName() if hasattr(w, "objectName") else "",
                "geometry": (geom.x(), geom.y(), geom.width(), geom.height()),
                "width": w.width(),
                "height": w.height(),
                "sizeHint": (size_hint.width(), size_hint.height()),
                "minimumSizeHint": (min_size_hint.width(), min_size_hint.height()),
                "minimumWidth": w.minimumWidth(),
                "maximumWidth": w.maximumWidth(),
                "minimumHeight": w.minimumHeight(),
                "maximumHeight": w.maximumHeight(),
                "visible": bool(w.isVisible()),
            }
        except Exception:
            return {"error": "unable to inspect widget"}

    def _print_widget(self, tag, w, label=None):
        lbl = label or getattr(w, "objectName", lambda: "")()
        info = self._widget_geom_summary(w)
        print(f"[{tag}] {lbl} -> {info.get('class', '?')}")
        for k, v in info.items():
            if k == "class":
                continue
            print(f"    {k}: {v}")
        # contentsRect when available
        try:
            if hasattr(w, "contentsRect"):
                cr = w.contentsRect()
                print(f"    contentsRect: ({cr.x()}, {cr.y()}, {cr.width()}, {cr.height()})")
        except Exception:
            pass
        # For QLabel-like widgets, print wordWrap and heightForWidth info
        try:
            from PyQt5.QtWidgets import QLabel

            if isinstance(w, QLabel):
                try:
                    print(f"    wordWrap: {w.wordWrap()}")
                except Exception:
                    pass
                try:
                    if w.hasHeightForWidth():
                        hw = w.heightForWidth(max(1, w.width()))
                        print(f"    heightForWidth(@{w.width()}): {hw}")
                except Exception:
                    pass
        except Exception:
            pass

    def _dump_layout(self, tag, layout):
        try:
            lm = layout.contentsMargins()
            print(f"[{tag}] layout -> contentsMargins: (l={lm.left()}, t={lm.top()}, r={lm.right()}, b={lm.bottom()}) spacing={layout.spacing()} count={layout.count()}")
        except Exception:
            print(f"[{tag}] layout -> (unable to inspect)")

    def _debug_dump(self, tag: str = "dump") -> None:
        """Traverse the inspector widget tree and print requested metrics.

        This function is intentionally verbose and for temporary debugging
        only. It prints widget geometry, size hints, layout margins/spacing,
        and scrollbar state for the inspector content.
        """
        print(f"--- INSPECTOR DEBUG DUMP [{tag}] ---")
        try:
            # Main window
            try:
                self._print_widget(tag, self, "WindowVisualizer")
            except Exception:
                print(f"[{tag}] WindowVisualizer: unavailable")

            inspector = getattr(self, "_inspector_shell", None)
            if inspector is None:
                print(f"[{tag}] Inspector shell not yet assigned")
            else:
                self._print_widget(tag, inspector, "InspectorShell")
                # Inspector -> TabPanel
                try:
                    panel = inspector.panel()
                    self._print_widget(tag, panel, "TabPanel")
                except Exception:
                    panel = None
                    print(f"[{tag}] TabPanel: unavailable")

                if panel is not None:
                    # content_scroll
                    try:
                        cs = panel._content_scroll
                        self._print_widget(tag, cs, "TabPanel._content_scroll")
                        try:
                            vp = cs.viewport()
                            self._print_widget(tag, vp, "TabPanel._content_scroll.viewport")
                        except Exception:
                            vp = None
                            print(f"[{tag}] content_scroll.viewport(): unavailable")
                    except Exception:
                        cs = None
                        vp = None
                        print(f"[{tag}] _content_scroll: unavailable")

                    # content_widget and layout
                    try:
                        cwidget = panel._content_widget
                        clayout = panel._content_layout
                        self._print_widget(tag, cwidget, "TabPanel._content_widget")
                        self._dump_layout("TabPanel._content_layout", clayout)
                    except Exception:
                        cwidget = None
                        clayout = None
                        print(f"[{tag}] _content_widget/_content_layout: unavailable")

                    # CollapsibleSections
                    try:
                        secs = getattr(panel, "_sections", [])
                        total_visible_sizehint = 0
                        print(f"[{tag}] CollapsibleSection count: {len(secs)}")
                        for i, sec in enumerate(secs):
                            vis = bool(sec.isVisible())
                            sh = sec.sizeHint()
                            total_visible_sizehint += sh.height() if vis else 0
                            print(f"[{tag}] Section[{i}] title={getattr(sec,'_title',None)} visible={vis} sizeHint=({sh.width()},{sh.height()})")
                            try:
                                self._print_widget(tag, sec._header_row, f"Section[{i}]._header_row")
                            except Exception:
                                pass
                            try:
                                self._print_widget(tag, sec._body_band, f"Section[{i}]._body_band")
                            except Exception:
                                pass
                            try:
                                self._print_widget(tag, sec._body, f"Section[{i}]._body")
                                self._dump_layout(f"Section[{i}]_body_layout", sec._body_layout)
                            except Exception:
                                pass
                        # compare sum of visible section sizeHints vs content widget
                        try:
                            cw_sh = cwidget.sizeHint().height() if cwidget is not None else None
                            print(f"[{tag}] Sum visible CollapsibleSection sizeHints height = {total_visible_sizehint}")
                            print(f"[{tag}] TabPanel._content_widget.sizeHint().height = {cw_sh}")
                        except Exception:
                            pass
                    except Exception:
                        print(f"[{tag}] CollapsibleSection inspection failed")

                    # MetadataBlock and grid
                    try:
                        info_block = getattr(self, "_info_block", None)
                        if info_block is None:
                            print(f"[{tag}] MetadataBlock (self._info_block) not found")
                        else:
                            self._print_widget(tag, info_block, "MetadataBlock")
                            gl = info_block.layout()
                            self._dump_layout("MetadataBlock.grid_layout", gl)
                            labels = info_block.labels()
                            sum_sizehint = 0
                            sum_allocated = 0
                            for key, lbl in labels.items():
                                try:
                                    self._print_widget(tag, lbl, f"Metadata key={key}")
                                except Exception:
                                    pass
                                # compare sizeHint/heightForWidth vs allocated geometry
                                try:
                                    sh = lbl.sizeHint().height()
                                    geom_h = lbl.geometry().height()
                                    hfw = lbl.heightForWidth(max(1, lbl.width())) if hasattr(lbl, "hasHeightForWidth") and lbl.hasHeightForWidth() else None
                                    print(f"[{tag}] key={key}: sizeHint.h={sh} heightForWidth={hfw} allocated.h={geom_h}")
                                    try:
                                        sum_sizehint += int(sh)
                                    except Exception:
                                        pass
                                    try:
                                        sum_allocated += int(geom_h)
                                    except Exception:
                                        pass
                                except Exception:
                                    pass
                            try:
                                print(f"[{tag}] MetadataBlock: sum value-label sizeHint heights = {sum_sizehint}, sum allocated heights = {sum_allocated}, block geom.h = {info_block.geometry().height()}")
                            except Exception:
                                pass
                    except Exception:
                        print(f"[{tag}] MetadataBlock inspection failed")

                    # Inspector vertical scrollbar
                    try:
                        sb = panel.content_scrollbar()
                        if sb is not None:
                            print(f"[{tag}] Inspector vertical scrollbar:")
                            try:
                                self._print_widget(tag, sb, "Inspector.vscrollbar")
                                print(f"    visible: {bool(sb.isVisible())}")
                                print(f"    minimum: {sb.minimum()} maximum: {sb.maximum()} pageStep: {sb.pageStep()} value: {sb.value()}")
                            except Exception:
                                pass
                        else:
                            print(f"[{tag}] content_scrollbar(): None")
                    except Exception:
                        print(f"[{tag}] Inspector scrollbar inspection failed")

                    # Comparisons requested
                    try:
                        vp_h = vp.height() if vp is not None else None
                        cw_sh = cwidget.sizeHint().height() if cwidget is not None else None
                        cw_h = cwidget.height() if cwidget is not None else None
                        print(f"[{tag}] Comparison: TabPanel content_widget.sizeHint.h={cw_sh} vs scroll viewport.h={vp_h}")
                        if cw_sh is not None and vp_h is not None:
                            if cw_sh > vp_h:
                                print(f"[{tag}] content_widget.sizeHint > viewport.h -> content likely taller than viewport")
                            elif cw_sh < vp_h:
                                print(f"[{tag}] content_widget.sizeHint < viewport.h -> content smaller than viewport; grey area likely leftover viewport space")
                            else:
                                print(f"[{tag}] content_widget.sizeHint == viewport.h")

                        print(f"[{tag}] content_widget.actual.height={cw_h} viewport.h={vp_h}")
                        if cw_h is not None and vp_h is not None:
                            if cw_h > vp_h:
                                print(f"[{tag}] content_widget actual height > viewport -> content is taller than viewport")
                            else:
                                print(f"[{tag}] content_widget actual height <= viewport -> viewport has leftover space")
                    except Exception:
                        pass

        except Exception:
            import traceback

            traceback.print_exc()

    # -------------------- Detailed capture & comparison --------------------
    def _capture_inspector_state(self) -> dict:
        """Capture a structured snapshot of the inspector and its contents.

        Returns a dict with numeric geometry/sizeHint/layout metrics useful
        for automated comparison between two selections.
        """
        state: dict = {}
        try:
            # Selected record metadata (best-effort)
            sel = None
            try:
                page = getattr(self, "_movie_page", None)
                if page is not None and hasattr(page, "_selected_index") and page._selected_index >= 0:
                    sel = page._records[page._selected_index]
            except Exception:
                sel = None
            state["selected"] = {
                "title": str(sel.get("title") or "") if isinstance(sel, dict) else "",
                "filename": str(sel.get("filename") or "") if isinstance(sel, dict) else "",
            }

            # Helper to safely summarize a widget
            def safe_summary(w):
                try:
                    return self._widget_geom_summary(w)
                except Exception:
                    return {}

            # Window / inspector / tabpanel
            state["window"] = safe_summary(self)
            inspector = getattr(self, "_inspector_shell", None)
            state["inspector"] = safe_summary(inspector) if inspector is not None else None

            panel = None
            try:
                panel = inspector.panel() if inspector is not None else None
            except Exception:
                panel = None
            state["tabpanel"] = safe_summary(panel) if panel is not None else None

            # content_scroll and viewport
            try:
                cs = panel._content_scroll if panel is not None else None
                state["content_scroll"] = safe_summary(cs) if cs is not None else None
                vp = cs.viewport() if cs is not None else None
                state["viewport"] = safe_summary(vp) if vp is not None else None
            except Exception:
                state["content_scroll"] = None
                state["viewport"] = None

            # content_widget and layout
            try:
                cwidget = panel._content_widget if panel is not None else None
                cw_sum = safe_summary(cwidget) if cwidget is not None else None
                try:
                    if cwidget is not None and hasattr(cwidget, "contentsRect"):
                        cr = cwidget.contentsRect()
                        cw_sum["contentsRect"] = (cr.x(), cr.y(), cr.width(), cr.height())
                except Exception:
                    pass
                state["content_widget"] = cw_sum
                try:
                    clayout = panel._content_layout if panel is not None else None
                    if clayout is not None:
                        lm = clayout.contentsMargins()
                        state["content_layout"] = {
                            "margins": (lm.left(), lm.top(), lm.right(), lm.bottom()),
                            "spacing": clayout.spacing(),
                            "count": clayout.count(),
                        }
                    else:
                        state["content_layout"] = None
                except Exception:
                    state["content_layout"] = None
            except Exception:
                state["content_widget"] = None
                state["content_layout"] = None

            # Collapsible sections
            secs_summary = []
            total_visible_sizehint = 0
            try:
                secs = getattr(panel, "_sections", []) if panel is not None else []
                for sec in secs:
                    try:
                        sh = sec.sizeHint()
                        vis = bool(sec.isVisible())
                        secinfo = {
                            "title": getattr(sec, "_title", None),
                            "visible": vis,
                            "sizeHint": (sh.width(), sh.height()),
                            "minimumSizeHint": (sec.minimumSizeHint().width(), sec.minimumSizeHint().height()),
                        }
                        try:
                            secinfo["header_row"] = safe_summary(sec._header_row)
                        except Exception:
                            secinfo["header_row"] = None
                        try:
                            secinfo["body_band"] = safe_summary(sec._body_band)
                            # body_band fixed height if present
                            try:
                                secinfo["body_band"]["fixedHeight"] = sec._body_band.height()
                            except Exception:
                                pass
                        except Exception:
                            secinfo["body_band"] = None
                        try:
                            secinfo["body"] = safe_summary(sec._body)
                        except Exception:
                            secinfo["body"] = None
                        if vis:
                            try:
                                total_visible_sizehint += int(sh.height())
                            except Exception:
                                pass
                        secs_summary.append(secinfo)
                    except Exception:
                        pass
            except Exception:
                secs_summary = []
            state["sections"] = secs_summary
            state["sum_visible_section_sizehint"] = total_visible_sizehint

            # MetadataBlock and rows
            try:
                info_block = getattr(self, "_info_block", None)
                if info_block is None:
                    state["metadata_block"] = None
                else:
                    mb = {}
                    mb["geom"] = safe_summary(info_block)
                    try:
                        gl = info_block.layout()
                        lm = gl.contentsMargins()
                        mb["grid_layout"] = {
                            "margins": (lm.left(), lm.top(), lm.right(), lm.bottom()),
                            "spacing": gl.spacing(),
                            "count": gl.count(),
                        }
                    except Exception:
                        mb["grid_layout"] = None

                    # rows and labels
                    try:
                        rows = getattr(info_block, "_rows", list(info_block.labels().keys()))
                    except Exception:
                        rows = list(info_block.labels().keys()) if info_block is not None else []
                    labels = info_block.labels() if info_block is not None else {}
                    row_entries = []
                    sum_sizehint = 0
                    sum_alloc = 0
                    for ri, key in enumerate(rows):
                        r = {"field": key}
                        try:
                            # key label from grid layout if possible
                            try:
                                item_key = gl.itemAtPosition(ri, 0) if gl is not None else None
                                key_widget = item_key.widget() if item_key is not None else None
                                r["key_label"] = safe_summary(key_widget) if key_widget is not None else None
                            except Exception:
                                r["key_label"] = None
                            val_widget = labels.get(key)
                            r["value_label"] = safe_summary(val_widget) if val_widget is not None else None
                            # heightForWidth
                            try:
                                if val_widget is not None and hasattr(val_widget, "hasHeightForWidth") and val_widget.hasHeightForWidth():
                                    r["value_heightForWidth"] = val_widget.heightForWidth(max(1, val_widget.width()))
                                else:
                                    r["value_heightForWidth"] = None
                            except Exception:
                                r["value_heightForWidth"] = None
                            try:
                                r["value_wordWrap"] = bool(val_widget.wordWrap()) if val_widget is not None else None
                            except Exception:
                                r["value_wordWrap"] = None
                            try:
                                r["allocated_height"] = val_widget.geometry().height() if val_widget is not None else None
                            except Exception:
                                r["allocated_height"] = None
                            try:
                                sh = val_widget.sizeHint().height() if val_widget is not None else 0
                                sum_sizehint += int(sh)
                            except Exception:
                                pass
                            try:
                                if r["allocated_height"] is not None:
                                    sum_alloc += int(r["allocated_height"])
                            except Exception:
                                pass
                        except Exception:
                            pass
                        row_entries.append(r)
                    mb["rows"] = row_entries
                    mb["sum_value_label_sizeHint_heights"] = sum_sizehint
                    mb["sum_allocated_heights"] = sum_alloc
                    try:
                        mb["block_geom_height"] = info_block.geometry().height()
                    except Exception:
                        mb["block_geom_height"] = None
                    mb["total_rows"] = len(row_entries)
                    state["metadata_block"] = mb
            except Exception:
                state["metadata_block"] = None

            # Inspector scrollbar
            try:
                sb = panel.content_scrollbar() if panel is not None and hasattr(panel, "content_scrollbar") else None
                if sb is not None:
                    sbsum = safe_summary(sb)
                    try:
                        sbsum.update({
                            "visible": bool(sb.isVisible()),
                            "minimum": sb.minimum(),
                            "maximum": sb.maximum(),
                            "pageStep": sb.pageStep(),
                            "value": sb.value(),
                        })
                    except Exception:
                        pass
                    state["scrollbar"] = sbsum
                else:
                    state["scrollbar"] = None
            except Exception:
                state["scrollbar"] = None

        except Exception:
            import traceback

            traceback.print_exc()
        return state

    def _print_case_state(self, state: dict, label: str) -> None:
        """Print a structured case dump (per the user's request)."""
        print(f"\n=== {label} ===")
        try:
            sel = state.get("selected", {})
            print(f"Selected: title=\"{sel.get('title','')}\" filename=\"{sel.get('filename','')}\"")

            # Window / Inspector / TabPanel
            print("\n-- Window / Inspector / TabPanel --")
            try:
                w = state.get("window")
                print(f"Window: geometry={w.get('geometry')} sizeHint={w.get('sizeHint')} minimumSizeHint={w.get('minimumSizeHint')}")
            except Exception:
                pass
            try:
                ins = state.get("inspector")
                print(f"Inspector: geometry={ins.get('geometry')} sizeHint={ins.get('sizeHint')} minimumSizeHint={ins.get('minimumSizeHint')}")
            except Exception:
                pass
            try:
                tp = state.get("tabpanel")
                print(f"TabPanel: geometry={tp.get('geometry')} sizeHint={tp.get('sizeHint')} minimumSizeHint={tp.get('minimumSizeHint')}")
            except Exception:
                pass

            # content scroll / viewport
            print("\n-- Scroll area / content widget --")
            try:
                cs = state.get("content_scroll")
                print(f"TabPanel._content_scroll: geometry={cs.get('geometry')} sizeHint={cs.get('sizeHint')}")
            except Exception:
                pass
            try:
                vp = state.get("viewport")
                print(f"TabPanel._content_scroll.viewport: geometry={vp.get('geometry')} sizeHint={vp.get('sizeHint')}")
            except Exception:
                pass
            try:
                cw = state.get("content_widget")
                print(f"TabPanel._content_widget: geometry={cw.get('geometry')} sizeHint={cw.get('sizeHint')} minimumSizeHint={cw.get('minimumSizeHint')} contentsRect={cw.get('contentsRect')}")
            except Exception:
                pass
            try:
                cl = state.get("content_layout")
                print(f"TabPanel._content_layout: margins={cl.get('margins')} spacing={cl.get('spacing')} count={cl.get('count')}")
            except Exception:
                pass

            # Collapsible sections
            print("\n-- Collapsible Sections --")
            try:
                for i, sec in enumerate(state.get("sections", [])):
                    print(f"Section[{i}] title={sec.get('title')} visible={sec.get('visible')} sizeHint={sec.get('sizeHint')} minimumSizeHint={sec.get('minimumSizeHint')}")
                    try:
                        hr = sec.get("header_row")
                        print(f"  header_row: geometry={hr.get('geometry')} sizeHint={hr.get('sizeHint')}")
                    except Exception:
                        pass
                    try:
                        bb = sec.get("body_band")
                        if bb is not None:
                            print(f"  body_band: geometry={bb.get('geometry')} fixedHeight={bb.get('fixedHeight')}")
                    except Exception:
                        pass
                    try:
                        bd = sec.get("body")
                        if bd is not None:
                            print(f"  body: geometry={bd.get('geometry')} sizeHint={bd.get('sizeHint')} minimumSizeHint={bd.get('minimumSizeHint')}")
                    except Exception:
                        pass
            except Exception:
                pass

            # MetadataBlock and rows
            print("\n-- MetadataBlock --")
            try:
                mb = state.get("metadata_block")
                if mb is None:
                    print("MetadataBlock: not present")
                else:
                    g = mb.get("geom")
                    print(f"MetadataBlock: geometry={g.get('geometry')} sizeHint={g.get('sizeHint')} minimumSizeHint={g.get('minimumSizeHint')}")
                    try:
                        gl = mb.get("grid_layout")
                        print(f"grid_layout: margins={gl.get('margins')} spacing={gl.get('spacing')} count={gl.get('count')}")
                    except Exception:
                        pass
                    print(f"total_rows: {mb.get('total_rows')} sum_value_label_sizeHint_heights={mb.get('sum_value_label_sizeHint_heights')} sum_allocated_heights={mb.get('sum_allocated_heights')} block_geom_height={mb.get('block_geom_height')}")
                    print("\nRows:")
                    for r in mb.get("rows", []):
                        kl = r.get("key_label")
                        vl = r.get("value_label")
                        print(f" field={r.get('field')}")
                        if kl is not None:
                            print(f"   key_label: geometry={kl.get('geometry')} sizeHint={kl.get('sizeHint')}")
                        if vl is not None:
                            print(f"   value_label: geometry={vl.get('geometry')} sizeHint={vl.get('sizeHint')} heightForWidth={r.get('value_heightForWidth')} wordWrap={r.get('value_wordWrap')} allocated_height={r.get('allocated_height')}")
            except Exception:
                pass

            # Scrollbar
            print("\n-- Inspector vertical scrollbar --")
            try:
                sb = state.get("scrollbar")
                if sb is None:
                    print("Inspector vertical scrollbar: None")
                else:
                    print(f"visible={sb.get('visible')} geometry={sb.get('geometry')} minimum={sb.get('minimum')} maximum={sb.get('maximum')} pageStep={sb.get('pageStep')} value={sb.get('value')}")
                    if sb.get('minimum') is not None and sb.get('maximum') is not None:
                        rng = sb.get('maximum') - sb.get('minimum')
                        print(f"scrollbar range non-zero: {rng != 0}")
            except Exception:
                pass
        except Exception:
            import traceback

            traceback.print_exc()

    def _compare_and_summarize(self, good: dict, bad: dict) -> None:
        """Compare two captured states and print the requested calculations and a short summary."""
        print("\n--- COMPARISON SUMMARY ---")
        try:
            # 1. Compare sum of visible CollapsibleSection sizeHint heights
            g_sum = int(good.get("sum_visible_section_sizehint") or 0)
            b_sum = int(bad.get("sum_visible_section_sizehint") or 0)
            print(f"Visible CollapsibleSection sizeHint sum: GOOD={g_sum} BAD={b_sum} diff={b_sum - g_sum}")

            # 2. Compare TabPanel._content_widget.sizeHint().height vs viewport height
            try:
                g_cw_sh = (good.get("content_widget") or {}).get("sizeHint")
                b_cw_sh = (bad.get("content_widget") or {}).get("sizeHint")
                g_cw_sh_h = g_cw_sh[1] if g_cw_sh is not None else None
                b_cw_sh_h = b_cw_sh[1] if b_cw_sh is not None else None
            except Exception:
                g_cw_sh_h = b_cw_sh_h = None
            try:
                g_vp_h = (good.get("viewport") or {}).get("height")
                b_vp_h = (bad.get("viewport") or {}).get("height")
            except Exception:
                g_vp_h = b_vp_h = None
            print(f"TabPanel.content_widget.sizeHint.h: GOOD={g_cw_sh_h} BAD={b_cw_sh_h}")
            print(f"Viewport.h: GOOD={g_vp_h} BAD={b_vp_h}")

            # 3. Compare MetadataBlock total label height vs MetadataBlock geometry height
            try:
                g_mb = good.get("metadata_block") or {}
                b_mb = bad.get("metadata_block") or {}
                print(f"MetadataBlock sums: GOOD sum_sizeHint={g_mb.get('sum_value_label_sizeHint_heights')} alloc_sum={g_mb.get('sum_allocated_heights')} geom_h={g_mb.get('block_geom_height')}")
                print(f"MetadataBlock sums: BAD  sum_sizeHint={b_mb.get('sum_value_label_sizeHint_heights')} alloc_sum={b_mb.get('sum_allocated_heights')} geom_h={b_mb.get('block_geom_height')}")
            except Exception:
                pass

            # 4. For each row, compare value label heightForWidth vs allocated height
            first_diff_row = None
            try:
                g_rows = (g_mb.get('rows') if g_mb is not None else []) or []
                b_rows = (b_mb.get('rows') if b_mb is not None else []) or []
                max_rows = max(len(g_rows), len(b_rows))
                for i in range(max_rows):
                    gr = g_rows[i] if i < len(g_rows) else None
                    br = b_rows[i] if i < len(b_rows) else None
                    if gr is None or br is None:
                        if first_diff_row is None:
                            first_diff_row = (i, gr, br)
                        continue
                    g_hfw = gr.get('value_heightForWidth')
                    b_hfw = br.get('value_heightForWidth')
                    g_alloc = gr.get('allocated_height')
                    b_alloc = br.get('allocated_height')
                    # Print per-row comparison
                    print(f"Row[{i}] field={br.get('field') if br else (gr.get('field') if gr else '')} heightForWidth: GOOD={g_hfw} BAD={b_hfw} allocated: GOOD={g_alloc} BAD={b_alloc}")
                    # Material difference threshold
                    try:
                        if first_diff_row is None:
                            if (g_hfw is not None and b_hfw is not None and abs((b_hfw or 0) - (g_hfw or 0)) >= 2) or (g_alloc is not None and b_alloc is not None and abs((b_alloc or 0) - (g_alloc or 0)) >= 2):
                                first_diff_row = (i, gr, br)
                    except Exception:
                        pass
            except Exception:
                pass

            if first_diff_row is not None:
                i, gr, br = first_diff_row
                fld = br.get('field') if br else (gr.get('field') if gr else str(i))
                print(f"First materially different row: index={i} field={fld}")
            else:
                print("No materially different row detected")

            # 5. Determine likely cause
            cause = "unknown"
            try:
                # wrapped value label needing more height?
                wrapped_issue = False
                for r in (b_mb.get('rows') or []):
                    hfw = r.get('value_heightForWidth')
                    alloc = r.get('allocated_height')
                    if hfw is not None and alloc is not None and hfw > alloc + 1:
                        wrapped_issue = True
                        break
                if wrapped_issue:
                    cause = "wrapped value label needing more height"
                elif b_sum > g_sum + 1:
                    cause = "section body expanding unexpectedly"
                else:
                    # stale row height if allocated changed but hfw didn't
                    stale = False
                    for i in range(min(len(g_rows), len(b_rows))):
                        gr = g_rows[i]
                        br = b_rows[i]
                        if gr is None or br is None:
                            continue
                        if (gr.get('value_heightForWidth') == br.get('value_heightForWidth')) and (gr.get('allocated_height') != br.get('allocated_height')):
                            stale = True
                            break
                    if stale:
                        cause = "stale row height"
                    else:
                        cause = "other or multiple factors"
            except Exception:
                pass

            print(f"Likely cause: {cause}")
        except Exception:
            import traceback

            traceback.print_exc()

    # -------------------- MetadataBlock row sizing probe --------------------
    def _print_metadatablock_rows(self, record: object | None, click_id: int | None = None) -> None:
        """Print a compact, structured dump focused on MetadataBlock row geometry.

        This is intended for read-only diagnostics only.
        """
        try:
            from PyQt5.QtGui import QFontMetrics
            import math

            cid = click_id or 0
            title = "" if not isinstance(record, dict) else str(record.get("title") or "")
            filename = "" if not isinstance(record, dict) else str(record.get("filename") or "")
            heading = f"CLICK {cid}: {title or filename}"
            print(f"\n{heading}")

            info_block = getattr(self, "_info_block", None)
            if info_block is None:
                print("MetadataBlock: not present")
                return

            # Basic block summary
            try:
                gb = self._widget_geom_summary(info_block)
                cr = None
                try:
                    if hasattr(info_block, "contentsRect"):
                        r = info_block.contentsRect()
                        cr = (r.x(), r.y(), r.width(), r.height())
                except Exception:
                    cr = None
                print(f"MetadataBlock: geometry={gb.get('geometry')} sizeHint={gb.get('sizeHint')} minimumSizeHint={gb.get('minimumSizeHint')} contentsRect={cr}")
            except Exception:
                pass

            # Grid layout metrics
            try:
                gl = info_block.layout()
                lm = gl.contentsMargins()
                spacing = gl.spacing()
                count = gl.count()
                # derive rows/cols from item positions
                rows = set()
                cols = set()
                for i in range(count):
                    try:
                        r, c, rs, cs = gl.getItemPosition(i)
                        for rr in range(r, r + rs):
                            rows.add(rr)
                        for cc in range(c, c + cs):
                            cols.add(cc)
                    except Exception:
                        pass
                row_count = max(rows) + 1 if rows else 0
                col_count = max(cols) + 1 if cols else 0
                print(f"grid_layout: margins=({lm.left()},{lm.top()},{lm.right()},{lm.bottom()}) spacing={spacing} item_count={count} row_count={row_count} col_count={col_count}")
            except Exception:
                print("grid_layout: unavailable")

            # Rows: keyed by MetadataBlock._rows order
            try:
                rows_order = getattr(info_block, "_rows", list(info_block.labels().keys()))
            except Exception:
                rows_order = list(info_block.labels().keys())

            labels = info_block.labels()

            # Precompute font metrics for line height estimates
            sample_font = None
            try:
                any_val = next(iter(labels.values())) if labels else None
                if any_val is not None:
                    sample_font = any_val.font()
                else:
                    sample_font = None
            except Exception:
                sample_font = None
            fm = QFontMetrics(sample_font) if sample_font is not None else None

            sum_hfw = 0
            sum_alloc = 0
            sum_sh = 0

            for ri, key in enumerate(rows_order):
                try:
                    # key label widget
                    key_item = gl.itemAtPosition(ri, 0) if gl is not None else None
                    key_w = key_item.widget() if key_item is not None else None
                except Exception:
                    key_w = None
                val_w = labels.get(key)

                ksum = self._widget_geom_summary(key_w) if key_w is not None else None
                vsum = self._widget_geom_summary(val_w) if val_w is not None else None

                # Compute heightForWidth for value
                try:
                    if val_w is not None and hasattr(val_w, "hasHeightForWidth") and val_w.hasHeightForWidth():
                        hfw = val_w.heightForWidth(max(1, val_w.width()))
                    else:
                        hfw = val_w.sizeHint().height() if val_w is not None else None
                except Exception:
                    hfw = None

                # contentsRect
                try:
                    crect = val_w.contentsRect() if val_w is not None else None
                    crect = (crect.x(), crect.y(), crect.width(), crect.height()) if crect is not None else None
                except Exception:
                    crect = None

                # wordWrap
                try:
                    wwrap = bool(val_w.wordWrap()) if val_w is not None else None
                except Exception:
                    wwrap = None

                # allocated height and sizeHint
                try:
                    alloc_h = vsum.get("height") if vsum is not None else None
                except Exception:
                    alloc_h = None
                try:
                    sh_h = vsum.get("sizeHint")[1] if vsum is not None and vsum.get("sizeHint") is not None else None
                except Exception:
                    sh_h = None

                # row geometry as computed from key/value widget bottoms
                try:
                    ky = ksum.get("geometry")[1] if ksum and ksum.get("geometry") is not None else None
                    kh = ksum.get("geometry")[3] if ksum and ksum.get("geometry") is not None else None
                except Exception:
                    ky = kh = None
                try:
                    vy = vsum.get("geometry")[1] if vsum and vsum.get("geometry") is not None else None
                    vh = vsum.get("geometry")[3] if vsum and vsum.get("geometry") is not None else None
                except Exception:
                    vy = vh = None
                row_y = None
                row_h = None
                try:
                    ys = [y for y in (ky, vy) if y is not None]
                    bs = [y + h for y, h in ((ky, kh), (vy, vh)) if y is not None and h is not None]
                    if ys and bs:
                        row_y = min(ys)
                        row_h = max(bs) - row_y
                except Exception:
                    pass

                # print row summary
                print(f"Row[{ri}] field={key}")
                if ksum is not None:
                    print(f"  key_label: geometry={ksum.get('geometry')} sizeHint={ksum.get('sizeHint')} minimumSizeHint={ksum.get('minimumSizeHint')}")
                if vsum is not None:
                    print(f"  value_label: geometry={vsum.get('geometry')} sizeHint={vsum.get('sizeHint')} minimumSizeHint={vsum.get('minimumSizeHint')}")
                    print(f"    wordWrap={wwrap} heightForWidth={hfw} contentsRect={crect} allocated_height={alloc_h} sizeHint_h={sh_h} row_geom_h={row_h} row_min_h={max((ksum.get('minimumHeight') if ksum else 0),(vsum.get('minimumHeight') if vsum else 0))}")

                # accumulate sums
                try:
                    if hfw is not None:
                        sum_hfw += int(hfw)
                except Exception:
                    pass
                try:
                    if alloc_h is not None:
                        sum_alloc += int(alloc_h)
                except Exception:
                    pass
                try:
                    if sh_h is not None:
                        sum_sh += int(sh_h)
                except Exception:
                    pass

                # Overview specifics
                if key == "overview" and val_w is not None:
                    try:
                        text = val_w.text() or ""
                        plain_len = len(text)
                        explicit_breaks = text.count("\n")
                        # approximate displayed lines from heightForWidth
                        approx_lines_hfw = None
                        approx_lines_alloc = None
                        if hfw is not None and fm is not None:
                            approx_lines_hfw = math.ceil(float(hfw) / max(1, fm.lineSpacing()))
                        if alloc_h is not None and fm is not None:
                            approx_lines_alloc = math.floor(float(alloc_h) / max(1, fm.lineSpacing()))
                        clipped = False
                        try:
                            if alloc_h is not None and hfw is not None and alloc_h < hfw:
                                clipped = True
                        except Exception:
                            clipped = False
                        print(f"    OVERVIEW: text_len={plain_len} explicit_breaks={explicit_breaks} approx_lines_hfw={approx_lines_hfw} approx_lines_alloc={approx_lines_alloc} allocated_height={alloc_h} heightForWidth={hfw} sizeHint_h={sh_h} clipped={clipped}")
                    except Exception:
                        pass

            # grid-level comparisons
            try:
                block_h = info_block.geometry().height()
            except Exception:
                block_h = None
            print(f"Summary metrics: sum_heightForWidth={sum_hfw} sum_allocated={sum_alloc} sum_sizeHint={sum_sh} block_geom_h={block_h}")

            # Short summary heuristics per user's request
            try:
                likely = "unknown"
                # If any overview allocated < hfw -> allocated smaller
                overview_row = None
                for r in (info_block._rows if hasattr(info_block, '_rows') else list(labels.keys())):
                    if r == 'overview':
                        overview_row = r
                        break
                # compute overview metrics if present
                over_alloc = over_hfw = over_sh = None
                if overview_row is not None:
                    vw = labels.get('overview')
                    try:
                        vs = self._widget_geom_summary(vw)
                        over_alloc = vs.get('height')
                    except Exception:
                        over_alloc = None
                    try:
                        over_hfw = vw.heightForWidth(max(1, vw.width())) if vw is not None and hasattr(vw, 'hasHeightForWidth') and vw.hasHeightForWidth() else (vw.sizeHint().height() if vw is not None else None)
                    except Exception:
                        over_hfw = None
                    try:
                        over_sh = vw.sizeHint().height() if vw is not None else None
                    except Exception:
                        over_sh = None

                if over_alloc is not None and over_hfw is not None and over_alloc < over_hfw:
                    likely = 'allocated height smaller than heightForWidth (likely truncation)'
                elif over_sh is not None and over_sh < (over_hfw or 0):
                    likely = 'label reports too-small sizeHint'
                elif block_h is not None and sum_hfw and block_h < sum_hfw:
                    likely = 'grid layout under-allocating total needed height'
                else:
                    likely = 'no clear single cause; possibly layout interplay'

                print(f"Short summary: {likely}")
            except Exception:
                pass

        except Exception:
            import traceback

            traceback.print_exc()


    # WindowVisualizer hook: create the inspector shell widget
    def create_inspector(self) -> QWidget:
        # Build inspector and return its shell widget to the window shell.
        return self._build_inspector()

    # WindowVisualizer hook: create the browser widget
    def create_browser(self) -> QWidget:
        self._browser_stack = QStackedWidget()
        self._browser_stack.setContentsMargins(0, 0, 0, 0)

        self._movie_page = _MetadataBrowserPage(self._project_path, "movie", "Movies")
        self._gameplay_page = _MetadataBrowserPage(self._project_path, "gameplay", "Gameplay")
        self._movie_page.selectionChanged.connect(lambda rec: self._on_page_selection_changed("movie", rec))
        self._gameplay_page.selectionChanged.connect(lambda rec: self._on_page_selection_changed("gameplay", rec))
        self._movie_page.openRequested.connect(lambda rec: self._open_record_in_shotlist("movie", rec))
        self._gameplay_page.openRequested.connect(lambda rec: self._open_record_in_shotlist("gameplay", rec))

        self._browser_stack.addWidget(self._movie_page)
        self._browser_stack.addWidget(self._gameplay_page)
        return self._browser_stack

    def _fit_splitter_width(self) -> None:
        inspector_w = self._inspector_collapse_threshold()
        self._inspector_shell.setMinimumWidth(inspector_w)
        total_w = max(self.width(), 980)
        browser_w = max(1, total_w - inspector_w)
        self._splitter.setSizes([browser_w, inspector_w])
        self._saved_splitter_sizes = [browser_w, inspector_w]
        self._last_visible_splitter_sizes = [browser_w, inspector_w]
        self._inspector_restore_w = browser_w + inspector_w
        # Defer reflow & thumbnail update so layouts and splitter sizes
        # have settled; avoids initial clipping until the user drags.
        QTimer.singleShot(0, self._request_browser_reflow)
        QTimer.singleShot(0, self._update_thumbnail_preview)

    def _on_inspector_scrollbar_range_changed(self, _min: int, _max: int) -> None:
        # Reserve or release splitter width when the inspector scrollbar appears.
        visible = (_max > 0)
        if visible == getattr(self, "_inspector_scrollbar_visible", False):
            return

        sizes = list(self._splitter.sizes())
        if len(sizes) != 2:
            self._inspector_scrollbar_visible = visible
            return

        sb_w = theme.SCROLLBAR_W
        if visible:
            sizes[1] += sb_w
        else:
            sizes[1] = max(0, sizes[1] - sb_w)

        self._inspector_scrollbar_visible = visible
        try:
            self._splitter.setSizes(sizes)
        except Exception:
            pass

    def _sync_inspector_scrollbar_width(self) -> None:
        return

    def _inspector_collapse_threshold(self) -> int:
        fixed_w = 0
        if hasattr(self, "_source_tabs"):
            fixed_w = max(fixed_w, self._source_tabs.sizeHint().width())
        if hasattr(self, "_tools_section"):
            fixed_w = max(fixed_w, self._tools_section.sizeHint().width())

        self._inspector_collapse_w = max(_INSPECTOR_MIN_W, int(round(fixed_w * 0.5)))
        return self._inspector_collapse_w

    def _sync_inspector_auto_collapse(self) -> None:
        return

    def _request_browser_reflow(self) -> None:
        page = self._browser_stack.currentWidget()
        if isinstance(page, _MetadataBrowserPage):
            page.request_reflow()

    def _active_page(self) -> _MetadataBrowserPage | None:
        page = self._browser_stack.currentWidget()
        return page if isinstance(page, _MetadataBrowserPage) else None

    def _zoom_in_current_page(self) -> None:
        page = self._active_page()
        if page is not None:
            page._change_zoom(_ZOOM_STEP)

    def _zoom_out_current_page(self) -> None:
        page = self._active_page()
        if page is not None:
            page._change_zoom(-_ZOOM_STEP)

    def _current_page_record(self) -> dict | None:
        page = self._active_page()
        return page.current_record() if page is not None else None

    def _current_page_thumbnail_path(self) -> Path | None:
        page = self._active_page()
        return page.current_thumbnail_path() if page is not None else None

    def _update_thumbnail_preview(self) -> None:
        if not hasattr(self, "_thumbnail_label"):
            return
        path = self._current_page_thumbnail_path()
        self._current_thumbnail_path = path
        if path is None or not path.exists():
            self._thumbnail_label.setPixmap(QPixmap())
            self._thumbnail_label.setText("No thumbnail")
            self._thumbnail_label.setFixedHeight(140)
            return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._thumbnail_label.setPixmap(QPixmap())
            self._thumbnail_label.setText("No thumbnail")
            self._thumbnail_label.setFixedHeight(140)
            return

        # Compute available width inside the section *body* so the image
        # scales to the true content area (not the header or tab padding).
        # Use the scroll area's viewport width but subtract the content
        # widget's left/right contents margins so the image does not bleed
        # into the pane inset owned by the content widget.
        available_w = None
        try:
            from PyQt5.QtWidgets import QScrollArea
            node = self._thumbnail_section
            while node is not None:
                node = node.parentWidget()
                if node is None:
                    break
                if isinstance(node, QScrollArea):
                    vp_w = max(1, node.viewport().width())
                    try:
                        content = node.widget()
                        if content is not None and content.layout() is not None:
                            m = content.layout().contentsMargins()
                            vp_w = max(1, vp_w - (m.left() + m.right()))
                    except Exception:
                        pass
                    available_w = vp_w
                    break
        except Exception:
            available_w = None

        if available_w is None:
            try:
                body = getattr(self._thumbnail_section, "_body")
                available_w = max(1, body.contentsRect().width())
            except Exception:
                available_w = max(1, self._thumbnail_section.contentsRect().width())

        # Scale the pixmap to the available width and constrain the QLabel
        # so it cannot grow beyond that width. Keep a fixed height to
        # reserve vertical space equal to the scaled image height.
        scaled = pixmap.scaledToWidth(available_w, Qt.SmoothTransformation)
        self._thumbnail_label.setText("")
        self._thumbnail_label.setPixmap(scaled)
        try:
            self._thumbnail_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._thumbnail_label.setMaximumWidth(available_w)
        except Exception:
            pass
        self._thumbnail_label.setFixedHeight(max(1, scaled.height()))

    def _open_selected_in_shotlist(self) -> None:
        record = self._current_page_record()
        self._open_record_in_shotlist("movie" if self._browser_stack.currentIndex() == 0 else "gameplay", record)

    def _open_record_in_shotlist(self, fallback_media_type: str, record: object) -> None:
        if not isinstance(record, dict):
            return
        filename = str(record.get("filename") or "")
        if not filename:
            return
        media_type = str(record.get("media_type") or fallback_media_type)
        open_at_shot(self._project_path, filename, media_type=media_type)

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        self._request_browser_reflow()
        self._update_thumbnail_preview()

    def _toggle_inspector(self) -> None:
        if self._inspector_hidden:
            self._inspector_shell.show()
            self._inspector_shell.setMinimumWidth(self._inspector_collapse_w or _INSPECTOR_MIN_W)
            if self._last_visible_splitter_sizes:
                self._splitter.setSizes(self._last_visible_splitter_sizes)
            elif self._saved_splitter_sizes:
                self._splitter.setSizes(self._saved_splitter_sizes)
            else:
                self._fit_splitter_width()
            self._inspector_hidden = False
            self._inspector_auto_collapsed = False
            QTimer.singleShot(0, self._request_browser_reflow)
            QTimer.singleShot(0, self._update_thumbnail_preview)
            return

        self._saved_splitter_sizes = self._splitter.sizes()
        self._last_visible_splitter_sizes = list(self._saved_splitter_sizes)
        self._inspector_restore_w = sum(self._saved_splitter_sizes)
        self._inspector_shell.hide()
        self._splitter.setSizes([max(1, self.width() - 1), 0])
        self._inspector_hidden = True
        self._inspector_auto_collapsed = False
        QTimer.singleShot(0, self._request_browser_reflow)
        QTimer.singleShot(0, self._update_thumbnail_preview)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        QTimer.singleShot(0, self._request_browser_reflow)
        QTimer.singleShot(0, self._update_thumbnail_preview)

    def _on_page_selection_changed(self, media_type: str, record: object) -> None:
        self._selected_records[media_type] = record if isinstance(record, dict) else None
        source_tabs = getattr(self, "_source_tabs", None)
        if source_tabs is None:
            return
        current_page = "movie" if source_tabs.currentIndex() == 0 else "gameplay"
        if current_page == media_type:
            self._show_record(record if isinstance(record, dict) else None)

        # Temporary debug probe: when a poster is clicked in the Movies
        # browser, print a full inspector geometry dump for the newly
        # selected record. Run after a short delay so the event loop and
        # layout have settled. Do not change selection semantics.
        try:
            if media_type == "movie":
                # Capture values now to avoid closure issues
                title = "" if not isinstance(record, dict) else str(record.get("title") or "")
                filename = "" if not isinstance(record, dict) else str(record.get("filename") or "")

                def _click_dump(t=title, f=filename, rec=record):
                    try:
                        # Compact metadata-focused dump (row sizing probe)
                        try:
                            # Increment and capture a click id for labeling
                            try:
                                self._metadata_click_count += 1
                            except Exception:
                                pass
                            cid = getattr(self, "_metadata_click_count", 0)
                            # Schedule the metadata-only dump after layouts settle
                            try:
                                QTimer.singleShot(0, lambda: self._print_metadatablock_rows(rec, cid))
                            except Exception:
                                # fallback: call directly
                                self._print_metadatablock_rows(rec, cid)
                        except Exception:
                            import traceback

                            traceback.print_exc()
                    except Exception:
                        import traceback

                        traceback.print_exc()

                # Give the event loop a moment to settle so geometry is final.
                QTimer.singleShot(150, _click_dump)
        except Exception:
            pass

    def _sync_inspector_to_current_tab(self) -> None:
        source_tabs = getattr(self, "_source_tabs", None)
        if source_tabs is None:
            return
        current_index = source_tabs.currentIndex()
        current_page = "movie" if current_index == 0 else "gameplay"
        self._browser_stack.setCurrentIndex(current_index)
        self._show_record(self._selected_records.get(current_page))
        self._request_browser_reflow()
        self._update_thumbnail_preview()

    def _on_source_tab_changed(self, index: int) -> None:
        self._browser_stack.setCurrentIndex(index)
        self._sync_inspector_to_current_tab()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        QTimer.singleShot(0, self._request_browser_reflow)
        QTimer.singleShot(0, self._update_thumbnail_preview)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._request_browser_reflow)
        QTimer.singleShot(0, self._update_thumbnail_preview)

    def _show_record(self, record: dict | None) -> None:
        if record is None:
            for key in _INFO_ROWS:
                self._info_block.set(key, "—")
            self._update_thumbnail_preview()
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
        # Presentation: do not truncate long fields here. Let MetadataBlock
        # handle wrapping/height so full values are displayed. Load all
        # values in a single batch so the block can reset its sizing state
        # before measuring the new content.
        formatted = {key: _format_value(values.get(key)) for key in _INFO_ROWS}
        try:
            # Use the batch `load` API so previous row heights are cleared
            # and recomputation happens once.
            self._info_block.load(formatted)
        except Exception:
            for key in _INFO_ROWS:
                value = formatted.get(key)
                self._info_block.set(key, value)
        self._update_thumbnail_preview()

    # Keyboard handling (Esc, Ctrl+Q/W, Tab, Shift+Tab) is provided by
    # WindowVisualizer; do not reimplement it here.


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
