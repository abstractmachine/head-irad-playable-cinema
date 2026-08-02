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
from visualizers.components.tab_panel import TabPanel
from visualizers.components.metadata_block import MetadataBlock
from visualizers.components.thumbnail_manager import ThumbnailManager
from visualizers.components.flow_widget import FlowWidget
from visualizers.components.selection_manager import SelectionManager
from visualizers.components.zoom_manager import ZoomManager
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
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
_THUMB_SIZE = 120
_THUMB_GAP = theme.SECTION_GAP
_INSPECTOR_MIN_W = 280
_ZOOM_MIN = 0.60
_ZOOM_MAX = 7.00
_ZOOM_STEP = 0.40
_ZOOM_DEFAULT = 1.00
_THUMB_LOAD_SIZE = int(round(_THUMB_SIZE * _ZOOM_MAX))


def _zoom_key(media_type: str) -> str:
    return f"metadata_browser_zoom_{media_type}"


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
    # Character wrapping for unbroken strings is InspectorValue's
    # responsibility (applied automatically in MetadataBlock.set()/.load()).
    # This function only handles value-shape formatting (list/dict/str), not
    # presentation.
    if value is None or value == "":
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(_format_value(item) for item in value if item not in (None, "")) or "—"
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=True, sort_keys=True)
        except Exception:
            return str(value)
    return str(value)





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
        # ZoomManager owns zoom/view-state and keeps page._zoom in sync.
        initial_zoom = float(_prefs.get(_zoom_key(media_type), _ZOOM_DEFAULT) or _ZOOM_DEFAULT)
        self._zoom_manager = ZoomManager(
            self,
            initial_zoom,
            _ZOOM_MIN,
            _ZOOM_MAX,
            _ZOOM_STEP,
            persist_cb=lambda v: _prefs.set(_zoom_key(self._media_type), v),
        )
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
        return self._zoom_manager.zoom()

    def set_zoom(self, zoom: float, persist: bool = True) -> None:
        return self._zoom_manager.set_zoom(zoom, persist=persist)

    def _change_zoom(self, delta: float) -> None:
        return self._zoom_manager.change_zoom(delta)

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
        return self._zoom_manager.apply_zoom_to_items()

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
        self._current_thumbnail_paths: dict[str, Path | None] = {"movie": None, "gameplay": None}
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
        # (post-populate debug dump removed)

        # (comparative debug instrumentation removed)

    def _build_inspector(self) -> QWidget:
        """Build the shared `Inspector`: one `Tab`/`TabPanel` per media type.

        Each tab owns its own Thumbnail/Info/Tools sections bound to that
        media type's data — there is no shared, swapped-in-place content.
        Only the active tab is ever mounted (see `TabbedPanel`), so a
        hidden tab's Info block cannot affect the inspector's size hint.
        """
        inspector = Inspector(self)
        inspector.set_minimum_width(_INSPECTOR_MIN_W)

        self._thumbnail_labels: dict[str, QLabel] = {}
        self._thumbnail_sections: dict[str, CollapsibleSection] = {}
        self._info_blocks: dict[str, MetadataBlock] = {}
        self._tools_sections: dict[str, CollapsibleSection] = {}
        self._zoom_in_btns: dict[str, QPushButton] = {}
        self._zoom_out_btns: dict[str, QPushButton] = {}

        for media_type, title in (("movie", " Movies "), ("gameplay", " Gameplay ")):
            inspector.add_tab(self._build_source_tab_panel(media_type), title)

        self._inspector = inspector
        inspector.tabbed_panel().currentChanged.connect(self._on_source_tab_changed)

        return inspector

    def _build_source_tab_panel(self, media_type: str) -> TabPanel:
        """Build one Inspector tab (Thumbnail/Info/Tools) for *media_type*."""
        panel = TabPanel()

        # Thumbnail section
        thumbnail_wrap = QWidget()
        thumbnail_layout = QVBoxLayout(thumbnail_wrap)
        thumbnail_layout.setContentsMargins(0, 0, 0, 0)
        thumbnail_layout.setSpacing(0)
        thumbnail_label = QLabel("No thumbnail")
        thumbnail_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        # Let the TabPanel paint the canonical pane background; keep the
        # thumbnail label itself transparent so the panel shows through.
        # Ensure no internal margins/padding so the image sits flush with
        # the section body edges.
        thumbnail_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; background: transparent; margin: 0px; padding: 0px; border: none;"
        )
        thumbnail_label.setMinimumHeight(140)
        thumbnail_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        thumbnail_layout.addWidget(thumbnail_label)
        self._thumbnail_sections[media_type] = panel.add_section(
            "Thumbnail", thumbnail_wrap, pref_key="metadata_section_thumbnail"
        )
        self._thumbnail_labels[media_type] = thumbnail_label

        # Info section
        info_wrap = QWidget()
        info_layout = QVBoxLayout(info_wrap)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        info_block = MetadataBlock(_INFO_ROWS)
        info_layout.addWidget(info_block)
        panel.add_section("Info", info_wrap, pref_key="metadata_section_info")
        self._info_blocks[media_type] = info_block

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

        zoom_in_btn = QPushButton("Zoom +")
        zoom_in_btn.setFocusPolicy(Qt.NoFocus)
        zoom_in_btn.setStyleSheet(action_style)
        zoom_in_btn.clicked.connect(lambda _checked=False, mt=media_type: self._change_zoom(mt, _ZOOM_STEP))
        tools_grid.addWidget(zoom_in_btn, 0, 0)
        self._zoom_in_btns[media_type] = zoom_in_btn

        zoom_out_btn = QPushButton("Zoom -")
        zoom_out_btn.setFocusPolicy(Qt.NoFocus)
        zoom_out_btn.setStyleSheet(action_style)
        zoom_out_btn.clicked.connect(lambda _checked=False, mt=media_type: self._change_zoom(mt, -_ZOOM_STEP))
        tools_grid.addWidget(zoom_out_btn, 0, 1)
        self._zoom_out_btns[media_type] = zoom_out_btn

        shotlist_btn = QPushButton("Shotlist")
        shotlist_btn.setFocusPolicy(Qt.NoFocus)
        shotlist_btn.setStyleSheet(action_style)
        shotlist_btn.setToolTip("Open the selected movie or gameplay entry in Shotlist")
        shotlist_btn.clicked.connect(lambda _checked=False, mt=media_type: self._open_shotlist_for(mt))
        tools_grid.addWidget(shotlist_btn, 1, 0, 1, 2)

        tools_layout.addLayout(tools_grid)
        self._tools_sections[media_type] = panel.add_section("Tools", tools_wrap, pref_key="metadata_section_tools")

        return panel

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
        # Each tab's zoom buttons reflect only its own page's zoom state.
        self._movie_page._zoom_manager.zoomChanged.connect(lambda _z: self._update_zoom_buttons("movie"))
        self._gameplay_page._zoom_manager.zoomChanged.connect(lambda _z: self._update_zoom_buttons("gameplay"))
        # Initial button state
        QTimer.singleShot(0, lambda: self._update_zoom_buttons("movie"))
        QTimer.singleShot(0, lambda: self._update_zoom_buttons("gameplay"))
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
        QTimer.singleShot(0, lambda: self._update_thumbnail_preview(self._active_media_type()))

    def _sync_inspector_scrollbar_width(self) -> None:
        return

    def _inspector_collapse_threshold(self) -> int:
        fixed_w = self._inspector.tabbed_panel().tab_bar().sizeHint().width()
        for section in self._tools_sections.values():
            fixed_w = max(fixed_w, section.sizeHint().width())

        self._inspector_collapse_w = max(_INSPECTOR_MIN_W, int(round(fixed_w * 0.5)))
        return self._inspector_collapse_w

    def _sync_inspector_auto_collapse(self) -> None:
        return

    def _request_browser_reflow(self) -> None:
        page = self._browser_stack.currentWidget()
        if isinstance(page, _MetadataBrowserPage):
            page.request_reflow()

    def _active_media_type(self) -> str:
        return self._media_type_for_index(self._inspector.currentIndex())

    def _media_type_for_index(self, index: int) -> str:
        return "movie" if index == 0 else "gameplay"

    def _page_for(self, media_type: str) -> _MetadataBrowserPage:
        return self._movie_page if media_type == "movie" else self._gameplay_page

    def _change_zoom(self, media_type: str, delta: float) -> None:
        self._page_for(media_type)._change_zoom(delta)
        QTimer.singleShot(0, lambda: self._update_zoom_buttons(media_type))

    def _update_zoom_buttons(self, media_type: str) -> None:
        """Enable/disable *media_type*'s zoom buttons based on its page's limits."""
        zm = self._page_for(media_type)._zoom_manager
        eps = 1e-9
        self._zoom_in_btns[media_type].setEnabled(zm.zoom() < zm._max - eps)
        self._zoom_out_btns[media_type].setEnabled(zm.zoom() > zm._min + eps)

    def _update_thumbnail_preview(self, media_type: str) -> None:
        thumbnail_label = self._thumbnail_labels[media_type]
        path = self._page_for(media_type).current_thumbnail_path()
        self._current_thumbnail_paths[media_type] = path
        if path is None or not path.exists():
            thumbnail_label.setPixmap(QPixmap())
            thumbnail_label.setText("No thumbnail")
            thumbnail_label.setFixedHeight(140)
            return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            thumbnail_label.setPixmap(QPixmap())
            thumbnail_label.setText("No thumbnail")
            thumbnail_label.setFixedHeight(140)
            return

        # Compute available width inside the section *body* so the image
        # scales to the true content area (not the header or tab padding).
        # Use the scroll area's viewport width but subtract the content
        # widget's left/right contents margins so the image does not bleed
        # into the pane inset owned by the content widget.
        thumbnail_section = self._thumbnail_sections[media_type]
        available_w = None
        node = thumbnail_section
        while node is not None:
            node = node.parentWidget()
            if node is None:
                break
            if isinstance(node, QScrollArea):
                vp_w = max(1, node.viewport().width())
                content = node.widget()
                if content is not None and content.layout() is not None:
                    m = content.layout().contentsMargins()
                    vp_w = max(1, vp_w - (m.left() + m.right()))
                available_w = vp_w
                break

        if available_w is None:
            available_w = max(1, thumbnail_section.contentsRect().width())

        # Scale the pixmap to the available width and constrain the QLabel
        # so it cannot grow beyond that width. Keep a fixed height to
        # reserve vertical space equal to the scaled image height.
        scaled = pixmap.scaledToWidth(available_w, Qt.SmoothTransformation)
        thumbnail_label.setText("")
        thumbnail_label.setPixmap(scaled)
        thumbnail_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        thumbnail_label.setMaximumWidth(available_w)
        thumbnail_label.setFixedHeight(max(1, scaled.height()))

    def _open_shotlist_for(self, media_type: str) -> None:
        record = self._page_for(media_type).current_record()
        self._open_record_in_shotlist(media_type, record)

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
        self._update_thumbnail_preview(self._active_media_type())

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
            QTimer.singleShot(0, lambda: self._update_thumbnail_preview(self._active_media_type()))
            return

        self._saved_splitter_sizes = self._splitter.sizes()
        self._last_visible_splitter_sizes = list(self._saved_splitter_sizes)
        self._inspector_restore_w = sum(self._saved_splitter_sizes)
        self._inspector_shell.hide()
        self._splitter.setSizes([max(1, self.width() - 1), 0])
        self._inspector_hidden = True
        self._inspector_auto_collapsed = False
        QTimer.singleShot(0, self._request_browser_reflow)
        QTimer.singleShot(0, lambda: self._update_thumbnail_preview(self._active_media_type()))

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        QTimer.singleShot(0, self._request_browser_reflow)
        QTimer.singleShot(0, lambda: self._update_thumbnail_preview(self._active_media_type()))

    def _on_page_selection_changed(self, media_type: str, record: object) -> None:
        self._selected_records[media_type] = record if isinstance(record, dict) else None
        self._show_record(media_type, record if isinstance(record, dict) else None)

    def _sync_inspector_to_current_tab(self) -> None:
        self._request_browser_reflow()
        self._update_thumbnail_preview(self._active_media_type())

    def _on_source_tab_changed(self, index: int) -> None:
        self._browser_stack.setCurrentIndex(index)
        self._sync_inspector_to_current_tab()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        QTimer.singleShot(0, self._request_browser_reflow)
        QTimer.singleShot(0, lambda: self._update_thumbnail_preview(self._active_media_type()))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._request_browser_reflow)
        QTimer.singleShot(0, lambda: self._update_thumbnail_preview(self._active_media_type()))

    def _show_record(self, media_type: str, record: dict | None) -> None:
        info_block = self._info_blocks[media_type]
        if record is None:
            for key in _INFO_ROWS:
                info_block.set(key, "—")
            self._update_thumbnail_preview(media_type)
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
            info_block.load(formatted)
        except Exception:
            for key in _INFO_ROWS:
                info_block.set(key, formatted.get(key))
        self._update_thumbnail_preview(media_type)

    # Keyboard handling (Esc, Ctrl+Q/W, Tab, Shift+Tab) is provided by
    # WindowVisualizer; do not reimplement it here.


def run_visualizer(project_path: str) -> None:
    """Create the QApplication (if needed) and launch the metadata visualizer."""
    from visualizers.launcher import run_visualizer_window
    run_visualizer_window("metadata", lambda: MetadataVisualizer(project_path))
