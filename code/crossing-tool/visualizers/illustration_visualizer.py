#!/usr/bin/env python3
"""Illustration Visualizer — reference implementation of the Visualizer Framework.

This module demonstrates framework composition in production code:

- IllustrationBrowser provides canonical collection browsing and selection.
- IllustrationSource subclasses provide records.
- Inspector panels are composed from reusable sections/components.
- styles.theme provides shared visual language tokens.

Ownership model used here:

- Browser owns selection and browsing state.
- Source owns record access and caching.
- Inspector owns presentation and selection-scoped controls.
- Services own business logic.
- CLI commands remain canonical project operations.
- Metadata files own persistent project state.

UI actions in this visualizer should call existing services/CLI-backed flows
instead of duplicating project logic. Interactive editing tools are the only
expected case where no meaningful CLI equivalent exists.

This module remains a visualizer implementation, not a second project backend.

Catalog browser:
    Browse extracted silhouette objects by vocabulary label.
    Shows all transparent PNG objects already in the catalog.
    Left panel: label list → film/shot tree.
    Right panel: thumbnail grid of all objects for the selected entry.
    Clicking a thumbnail shows the full object and its metadata.

An "Open in Segmentation" action launches the Segmentation visualizer
(``visualizers.segmentation_visualizer.SAMExplorer``) as its own window for
interactive shot inspection — it is a separate visualizer module, not part
of this one.

Keyboard shortcuts (Catalog):
  Up / Down    — previous / next label
  Left / Right — previous / next object thumbnail (when grid focused)
  Escape / Ctrl+Q / Ctrl+W — close

Launched via:
    crossing visualizer illustration
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
from styles.theme import JumpScrollBar, save_window_geometry, restore_window_geometry
from visualizers.components.combo_popup import attach_combo_popup

# Fix Qt plugin conflict with OpenCV — del env var before first PyQt5 import
if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]

from PyQt5.QtCore import Qt, QEvent, QSize, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QSlider,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtWidgets import QSlider  # kept for any future use; not used in Catalog panel
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QImage,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)

from styles.theme import svg_icon as _svg_icon

# Framework components
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.illustration_browser import IllustrationBrowser
from visualizers.components.illustration_source import SilhouetteSource, EngravingSource
from visualizers.components.inspector import Inspector
from visualizers.components.tab_panel import TabPanel
from visualizers.components.hover_icon_button import HoverIconButton, build_icon_pair
from visualizers.components.ipc_server import IpcServer
from visualizers.components.metadata_block import MetadataBlock


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SIDE_PANE_W = 230   # combined inspector + filter side pane
# Splitter-pane floor for the inspector, matching Metadata's own
# `_INSPECTOR_MIN_W` (metadata_visualizer.py) so both visualizers' inspectors
# collapse to the same effective minimum width instead of Illustration being
# a wider special case. Kept as its own local constant (not imported from
# metadata_visualizer.py) so the two visualizers stay independently
# maintainable; if the values ever need to diverge again this is the only
# place to change for Illustration.
_INSPECTOR_MIN_W = 280
_DEFAULT_MODEL = "sam3.pt"   # still used as the segmentation-model fallback
                             # here ("Open in Segmentation" action + window
                             # default) even though SAMExplorer itself now
                             # lives in segmentation_visualizer.py.

# (display label, data key) pairs for the cascading sort dropdowns.
# Data keys map to  <key>_score  or  <key>  fields in catalog JSON records.
# "alphabetical" is a special case: sorts by label name (case-insensitive).
_SORT_OPTS: list[tuple[str, str]] = [
    ("confidence",     "confidence"),
    ("usefulness",     "usefulness"),
    ("engraving",      "engraving"),
    ("fullness",       "fullness"),
    ("size",           "size"),
    ("completeness",   "completeness"),
    ("isolation",      "isolation"),
    ("semantic label", "semantic_label"),
    ("semantic field", "semantic_field"),
    ("alphabetical",   "alphabetical"),
    ("engraved",       "engraved_first"),
]


# ---------------------------------------------------------------------------
# Catalog browser
# ---------------------------------------------------------------------------

_THUMB_SIZE  = 120   # px per thumbnail cell — passed to IllustrationBrowser

# Info table keys per source mode.
_SIL_INFO_KEYS = (
    "label", "film", "shot", "frame", "confidence",
    "usefulness", "fullness", "size", "overlap",
    "semantic_label", "semantic_field", "model",
)
_ENG_INFO_KEYS = (
    "label", "film", "mode", "model", "object_id",
)


# ---------------------------------------------------------------------------
# IPC — single-instance navigate socket
# ---------------------------------------------------------------------------

def _ill_ipc_socket_path(project_path: str) -> Path:
    """Return a per-project socket file path for the Illustration Visualizer."""
    import tempfile, hashlib
    h = hashlib.md5(str(project_path).encode()).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"crossing_illustration_{h}.sock"


class _IllIpcServer(IpcServer):
    """Listens on a Unix-domain socket and emits navigate_requested.

    The navigate message now includes `media_type` so callers can request
    a mode switch on the running instance before navigation. The socket
    accept/read/dispatch loop is provided by the shared `IpcServer` base;
    this subclass only supplies the socket path and message handling.
    """

    # film, field, label, shot_id, media_type
    navigate_requested = pyqtSignal(str, str, str, str, str)

    def __init__(self, project_path: str, parent=None) -> None:
        super().__init__(_ill_ipc_socket_path(project_path), parent)

    def _handle_message(self, msg: dict) -> None:
        if msg.get("action") == "navigate":
            self.navigate_requested.emit(
                msg.get("film", ""),
                msg.get("field", ""),
                msg.get("label", ""),
                msg.get("shot_id", ""),
                msg.get("media_type", ""),
            )


def _ill_ipc_send_navigate(
    project_path: str,
    film: str = "",
    field: str = "",
    label: str = "",
    shot_id: str = "",
    media_type: str = "",
) -> bool:
    """Send a navigate request to a running Illustration Visualizer.

    Returns True if the message was delivered, False if no server is listening.
    """
    import json as _json
    import socket as _socket
    sock_path = _ill_ipc_socket_path(project_path)
    if not sock_path.exists():
        return False
    try:
        conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        conn.settimeout(2.0)
        conn.connect(str(sock_path))
        msg = _json.dumps({
            "action":     "navigate",
            "film":       film,
            "field":      field,
            "label":      label,
            "shot_id":    shot_id,
            "media_type": media_type,
        })
        conn.sendall(msg.encode())
        conn.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------

class _DeleteWorker(QThread):
    """Delete an engraving directory in a background thread, then signal done."""
    finished = pyqtSignal()

    def __init__(self, dir_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._dir_path = dir_path

    def run(self) -> None:
        import shutil
        try:
            shutil.rmtree(str(self._dir_path))
        except Exception:
            pass
        self.finished.emit()


class _EngravingWorker(QThread):
    """Run a CLI engraving command in a background thread (single item)."""
    finished = pyqtSignal(bool, str)   # success, error_message

    def __init__(self, cmd: list, parent=None) -> None:
        super().__init__(parent)
        self._cmd = cmd

    def run(self) -> None:
        import subprocess
        try:
            result = subprocess.run(
                self._cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            ok  = result.returncode == 0
            err = result.stderr.strip() if not ok else ""
            if not ok:
                import sys as _sys
                print(f"[EngravingWorker] command failed (rc={result.returncode}):",
                      file=_sys.stderr)
                if err:
                    print(err, file=_sys.stderr)
            self.finished.emit(ok, err)
        except subprocess.TimeoutExpired:
            self.finished.emit(False, "Timed out after 10 min")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class _IllustrationIndexWorker(QThread):
    """Rebuild both Illustration browse indexes away from the GUI thread."""
    finished = pyqtSignal(bool, str)

    def __init__(self, project_path: str, media_type: str, parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._media_type = media_type

    def run(self) -> None:
        try:
            from services.illustration_index import rebuild_all
            results = rebuild_all(self._project_path, self._media_type)
            failed = [
                source for source, result in results.items()
                if result.get("status") != "ready"
            ]
            if failed:
                self.finished.emit(False, f"Index changed during rebuild: {', '.join(failed)}")
            else:
                self.finished.emit(True, "")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class _BatchEngravingWorker(QThread):
    """Run ``engraving batch`` in a background thread with line-by-line stdout.

    Supports cooperative cancellation via ``cancel()`` which terminates the
    subprocess.  Emits ``finished(success, error_message)`` when done.
    """
    finished = pyqtSignal(bool, str)

    def __init__(self, cmd: list, parent=None) -> None:
        super().__init__(parent)
        self._cmd       = cmd
        self._process   = None
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation — terminates the subprocess if running."""
        self._cancelled = True
        if self._process is not None:
            try:
                self._process.terminate()
            except Exception:
                pass

    def run(self) -> None:
        import subprocess, sys as _sys
        try:
            self._process = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # Drain stdout so the pipe never blocks.
            for line in self._process.stdout:
                if self._cancelled:
                    break
            self._process.wait()
            if self._cancelled:
                self.finished.emit(False, "Cancelled")
            else:
                ok  = self._process.returncode == 0
                err = self._process.stderr.read().strip() if not ok else ""
                if not ok:
                    print(f"[BatchEngravingWorker] rc={self._process.returncode}",
                          file=_sys.stderr)
                    if err:
                        print(err, file=_sys.stderr)
                self.finished.emit(ok, err)
        except Exception as exc:
            self.finished.emit(False, str(exc))


# Use shared HoverIconButton from visualizers.components.hover_icon_button


class IllustrationPane(QWidget):
    """Reference framework composition for silhouette/engraving browsing.

    Layout contract:
    - Left pane: browser surface (collection browsing + selection).
    - Right pane: inspector composition (sections reacting to selection).

    The pane wires browser signals to inspector and operation controls. It does
    not reimplement source scanning or service business logic.
    """

    def __init__(self, project_path: str, media_type: Optional[str] = None, parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._current_rec: Optional[dict] = None

        # Restore last-used media type; fall back to "movie" on first launch.
        if media_type is None:
            try:
                from tool import prefs as _prefs
                _saved = _prefs.get("ill_media_type")
                if _saved is None:
                    media_type = "movie"
                elif _saved == "":
                    media_type = None
                else:
                    media_type = _saved
            except Exception:
                media_type = "movie"

        # ── Sources ───────────────────────────────────────────────────────
        self._sil_source = SilhouetteSource(project_path)
        self._eng_source = EngravingSource(project_path)

        # ── Browsers (one per source; only the active one is visible) ─────
        _browser_kwargs = dict(thumb_size=_THUMB_SIZE, detach_controls=True)
        self._browser_sil = IllustrationBrowser(
            source=self._sil_source, media_type=media_type, **_browser_kwargs
        )
        self._browser_eng = IllustrationBrowser(
            source=self._eng_source, media_type=media_type, light_bg=True,
            auto_load=False, **_browser_kwargs
        )
        # Active browser alias — updated on tab switch.
        self._browser = self._browser_sil
        self._browser_sil.selectionChanged.connect(self._on_selection_changed)
        self._browser_sil.catalogReloaded.connect(self._update_eng_buttons)
        self._browser_eng.selectionChanged.connect(self._on_selection_changed)

        # Persist media-type selection across restarts (shared for both modes).
        self._browser_sil._media_combo.currentIndexChanged.connect(
            self._on_media_type_pref_changed
        )
        self._browser_eng._media_combo.currentIndexChanged.connect(
            self._on_media_type_pref_changed
        )

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        # NOTE: this pane has no splitter/layout of its own. `_browser_stack`
        # and the inspector widget (from `_build_inspector_pane()`) are
        # returned individually via `IllustrationWindow.create_browser()`/
        # `create_inspector()`, and it is the shared `WindowVisualizer` shell
        # that parents them into its own splitter and owns all width
        # negotiation. This pane must not maintain a competing splitter.
        self._browser_stack = QStackedWidget()
        self._browser_stack.addWidget(self._browser_sil)   # index 0
        self._browser_stack.addWidget(self._browser_eng)   # index 1

        # Double-click dispatches to the source-appropriate primary action.
        self._browser_sil.itemActivated.connect(self._open_in_shotlist)
        self._browser_eng.itemActivated.connect(self._open_engraving_in_viewer)

        self._build_inspector_pane()

    # --

    def _build_inspector_pane(self) -> QWidget:
        """Right pane: two source tabs (Silhouettes / Engravings), each with
        its own Filter/[Sort]/[Mode]/Info/Tools `TabPanel`, backed by the
        shared `TabbedPanel` so only the active tab is ever mounted (a
        hidden tab cannot influence sizeHint/scrollbar/gutter behavior).
        """
        inspector = Inspector(self)
        inspector.set_minimum_width(_SIDE_PANE_W)

        sil_panel, self._sil_sort_combo, self._sil_meta_rows = self._build_source_panel(
            self._browser_sil, "ill_sil", _SIL_INFO_KEYS, has_sort=True, has_tools=True
        )
        eng_panel, self._eng_sort_combo, self._eng_meta_rows = self._build_source_panel(
            self._browser_eng, "ill_eng", _ENG_INFO_KEYS,
            has_sort=False, has_mode_filter=True, has_eng_tools=True
        )

        inspector.add_tab(sil_panel, " Silhouettes ")
        inspector.add_tab(eng_panel, " Engravings ")

        self._sort_combo = self._sil_sort_combo
        self._meta_rows = self._sil_meta_rows

        inspector.tabbed_panel().currentChanged.connect(self._on_source_tab_changed)
        self._side_scroll = inspector

        return inspector

    # ------------------------------------------------------------------ helpers

    def _make_source_tab_panel(self) -> TabPanel:
        """Return a `TabPanel` styled/sized for one Illustration source tab."""
        _content_style = (
            f" QComboBox {{ background-color: {theme.BTN_BG}; color: {theme.TEXT}; }}"
            f" QComboBox::drop-down {{ border: none; }}"
            f" QComboBox QAbstractItemView, QComboBox QListView {{"
            f"   background: {theme.INPUT_BG}; color: {theme.TEXT};"
            f"   border: 0px; margin: 0px; padding: 0px; outline: 0px;"
            f"   selection-background-color: {theme.ACCENT};"
            f"   selection-color: {theme.ACCENT_TEXT}; }}"
            f" QComboBox QAbstractItemView::item, QComboBox QListView::item {{"
            f"   padding: 0px 8px; min-height: 24px; border: 0px; }}"
        )
        panel = TabPanel()
        # TabPanel already paints the canonical pane background/border;
        # layer the combo rules used throughout this source panel on top
        # rather than replacing the stylesheet outright. Buttons in this
        # panel each set their own stylesheet via `self._btn_style()`
        # (theme.action_button_stylesheet()), so no button rules are
        # layered here.
        panel.setStyleSheet(panel.styleSheet() + _content_style)
        panel.setMinimumWidth(_SIDE_PANE_W)
        return panel

    def _make_sort_combo(self, panel: TabPanel, pref_key: str) -> QComboBox:
        """Build, wire, and add a Sort section+combo to *panel*; return the combo."""
        sort_sec = CollapsibleSection("Sort", pref_key=pref_key)
        combo = QComboBox()
        combo.setFocusPolicy(Qt.NoFocus)
        combo.setMaxVisibleItems(10)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContentsOnFirstShow)
        # Configure popup view using shared helper
        attach_combo_popup(combo)
        combo.addItem("-----", userData=None)
        for disp, key in _SORT_OPTS:
            combo.addItem(disp, userData=key)

        def _refresh_color(_idx: int = 0, _c=combo) -> None:
            _col = theme.TEXT_DIM if _c.currentData() is None else theme.TEXT
            _c.setStyleSheet(
                f"QComboBox {{ background: {theme.BTN_BG}; color: {_col};"
                f" border: none; border-radius: 3px; padding: 0px 6px;"
                f" min-height: 24px; max-height: 24px; }}"
                f"QComboBox::drop-down {{ border: none; }}"
            )
        combo.currentIndexChanged.connect(_refresh_color)
        combo.currentIndexChanged.connect(self._on_sort_changed)
        _refresh_color()
        sort_sec.add_widget(combo)
        panel.add_widget(sort_sec)
        return combo

    # Popup attachment handled by visualizers.components.combo_popup.attach_combo_popup

    def _make_info_grid(self, panel: TabPanel, pref_key: str,
                        info_keys: tuple) -> dict:
        """Build and add an Info collapsible section; return {key: QLabel} dict."""
        info_sec = CollapsibleSection("Info", pref_key=pref_key)
        block = MetadataBlock(list(info_keys))
        info_sec.add_widget(block)
        panel.add_widget(info_sec)
        return block.labels()

    def _build_source_panel(
        self,
        browser: "IllustrationBrowser",
        pref_prefix: str,
        info_keys: tuple,
        has_sort: bool = True,
        has_mode_filter: bool = False,
        has_tools: bool = False,
        has_eng_tools: bool = False,
    ) -> tuple:
        """Build a complete Filter/[Sort]/[Mode]/Info/[Tools] `TabPanel` for
        *browser*.

        Returns (TabPanel, sort_combo, meta_rows).  sort_combo is None when
        has_sort is False.
        """
        panel = self._make_source_tab_panel()

        # ── Filter ────────────────────────────────────────────────────────
        filter_sec = CollapsibleSection("Filter",
                        pref_key=f"{pref_prefix}_section_filter")
        _fb = QWidget()
        _fb_lay = QVBoxLayout(_fb)
        _fb_lay.setContentsMargins(0, 0, 0, 0)
        _fb_lay.setSpacing(0)
        _fb_lay.addWidget(browser.filter_panel)
        _fb_lay.addWidget(browser.status_bar)
        _fb_lay.addWidget(browser.pagination_panel)
        filter_sec.add_widget(_fb)
        browser.keywordChanged.connect(
            lambda kw, sec=filter_sec: sec.set_subtitle(kw.capitalize() if kw else "")
        )
        panel.add_widget(filter_sec)
        filter_sec.set_subbar(browser._loading_bar)

        # ── Sort (optional) ───────────────────────────────────────────────
        sort_combo = None
        if has_sort:
            sort_combo = self._make_sort_combo(panel, f"{pref_prefix}_section_sort")

        # ── Mode filter (engravings) ──────────────────────────────────────
        if has_mode_filter:
            self._build_mode_filter_section(panel, browser)

        # ── Info ──────────────────────────────────────────────────────────
        meta_rows = self._make_info_grid(panel, f"{pref_prefix}_section_info", info_keys)

        # ── Tools ─────────────────────────────────────────────────────────
        if has_tools:
            self._build_tools_section(panel)
        if has_eng_tools:
            self._build_engraving_tools_section(panel)

        return panel, sort_combo, meta_rows

    # ------------------------------------------------------------------ shared button helpers

    def _btn_style(self) -> str:
        """Single canonical stylesheet for all action buttons in this visualizer.

        Delegates to the shared `theme.action_button_stylesheet()` so the
        font weight, hover, pressed, checked, and disabled states match
        Metadata and Project rather than diverging with a local copy.
        """
        return theme.action_button_stylesheet()

    def _make_btn_icon(self, svg_name: str, size: int = 14) -> tuple:
        """Return (normal_icon, hover_icon) for *svg_name* by delegating
        to the shared `build_icon_pair()` helper.
        """
        return build_icon_pair(svg_name, size, normal_color=theme.TEXT, hover_color=theme.ACCENT_TEXT)

    def _build_tools_section(self, panel: TabPanel) -> None:
        """Add the Tools collapsible section to *panel*."""
        tools_sec = CollapsibleSection("Tools", pref_key="ill_section_tools")
        _icon_sz = QSize(14, 14)
        _open_icon, _open_icon_hover = self._make_btn_icon("open-in-window", 14)

        _abtn = self._btn_style()
        _row = QWidget()
        _rl  = QHBoxLayout(_row)
        _rl.setContentsMargins(0, 0, 0, 0)
        _rl.setSpacing(theme.SECTION_GAP)

        self._best_btn = QPushButton("Best")
        self._best_btn.setCheckable(True)
        self._best_btn.setFocusPolicy(Qt.NoFocus)
        self._best_btn.setEnabled(False)
        self._best_btn.setFixedHeight(theme.BTN_H)
        self._best_btn.setStyleSheet(_abtn)
        self._best_btn.clicked.connect(self._on_best_btn_clicked)
        tools_sec.add_widget(self._best_btn)

        self._shotlist_btn = HoverIconButton("Shotlist", _open_icon, _open_icon_hover)
        self._shotlist_btn.setIconSize(_icon_sz)
        self._shotlist_btn.setFocusPolicy(Qt.NoFocus)
        self._shotlist_btn.setEnabled(False)
        self._shotlist_btn.setStyleSheet(_abtn)
        self._shotlist_btn.clicked.connect(self._open_in_shotlist)
        _rl.addWidget(self._shotlist_btn, 1)

        self._sam_btn = HoverIconButton("Segmentation", _open_icon, _open_icon_hover)
        self._sam_btn.setIconSize(_icon_sz)
        self._sam_btn.setFocusPolicy(Qt.NoFocus)
        self._sam_btn.setEnabled(False)
        self._sam_btn.setStyleSheet(_abtn)
        self._sam_btn.clicked.connect(self._open_sam_explorer)
        _rl.addWidget(self._sam_btn, 1)
        tools_sec.add_widget(_row)

        # ── Engraving generation row: [Engrave] [Engraving] ─────────────
        _e_icon, _e_icon_hover  = self._make_btn_icon("media-image-plus", 14)
        _v_icon, _v_icon_hover  = self._make_btn_icon("media-image",      14)
        _b_icon, _b_icon_hover  = self._make_btn_icon("media-image-plus", 14)
        _eng_row = QWidget()
        _eng_rl  = QHBoxLayout(_eng_row)
        _eng_rl.setContentsMargins(0, 0, 0, 0)
        _eng_rl.setSpacing(theme.SECTION_GAP)
        self._eng_gen_btn = HoverIconButton("Engrave", _e_icon, _e_icon_hover)
        self._eng_gen_btn.setIconSize(_icon_sz)
        self._eng_gen_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_gen_btn.setCheckable(True)   # stays highlighted while running
        self._eng_gen_btn.setEnabled(False)
        self._eng_gen_btn.setStyleSheet(_abtn)
        self._eng_gen_btn.clicked.connect(self._start_engraving_generation)
        _eng_rl.addWidget(self._eng_gen_btn, 1)
        self._eng_viz_btn = HoverIconButton("Engraving", _v_icon, _v_icon_hover)
        self._eng_viz_btn.setIconSize(_icon_sz)
        self._eng_viz_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_viz_btn.setEnabled(False)
        self._eng_viz_btn.setStyleSheet(_abtn)
        self._eng_viz_btn.clicked.connect(self._visualize_engraving)
        _eng_rl.addWidget(self._eng_viz_btn, 1)
        tools_sec.add_widget(_eng_row)
        self._eng_batch_btn = HoverIconButton("Generate All Marked", _b_icon, _b_icon_hover)
        self._eng_batch_btn.setIconSize(_icon_sz)
        self._eng_batch_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_batch_btn.setCheckable(True)   # stays highlighted while running
        self._eng_batch_btn.setEnabled(False)
        self._eng_batch_btn.setStyleSheet(_abtn)
        self._eng_batch_btn.clicked.connect(self._toggle_batch_generation)
        tools_sec.add_widget(self._eng_batch_btn)
        self._sil_rebuild_index_btn = self._make_rebuild_index_button()
        tools_sec.add_widget(self._sil_rebuild_index_btn)

        # Animation timer for "Generating…" dots
        self._eng_anim_timer = QTimer(self)
        self._eng_anim_timer.setInterval(400)
        self._eng_anim_timer.timeout.connect(self._tick_eng_animation)
        self._eng_anim_dots  = 0
        # Count-refresh timer: re-checks remaining pending items every 2 s
        # while the batch is running, giving a live countdown on the button.
        self._eng_count_timer = QTimer(self)
        self._eng_count_timer.setInterval(2000)
        self._eng_count_timer.timeout.connect(self._refresh_batch_count)
        self._eng_gen_running   = False
        self._eng_batch_running = False
        tools_sec._header.setToolTip(
            "Home / End \u2014 film\n"
            "PgUp / PgDn \u2014 field\n"
            "\u2190 \u2191 \u2192 \u2193 \u2014 grid\n"
            "a\u2013z / # \u2014 letter\n"
            "Shift+\u2191\u2193 \u2014 keyword\n"
            "Shift+\u2190\u2192 \u2014 page\n"
            "Enter \u2014 toggle best\n"
            "Shift+Enter \u2014 shotlist"
        )
        panel.add_widget(tools_sec)

    # ------------------------------------------------------------------ source switching

    def _on_source_tab_changed(self, idx: int) -> None:
        """Switch the active browser and inspector state when the tab changes."""
        if idx == 0:
            self._browser    = self._browser_sil
            self._meta_rows  = self._sil_meta_rows
            self._sort_combo = self._sil_sort_combo
        else:
            self._browser    = self._browser_eng
            self._meta_rows  = self._eng_meta_rows
            self._sort_combo = self._eng_sort_combo
            if not self._browser_eng._load_requested:
                self._browser_eng.reload()
        self._browser_stack.setCurrentIndex(idx)
        rec = self._browser.currentItem()
        if rec:
            self._on_selection_changed(rec)
        else:
            self._clear_meta()

    @property
    def _active_best_btn(self):
        """The Best button for the currently active source tab."""
        if self._browser is self._browser_eng:
            return getattr(self, "_eng_best_btn", None)
        return getattr(self, "_best_btn", None)

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        pass  # two-pane — no locking needed

    def _on_media_type_pref_changed(self, _idx: int) -> None:
        """Persist the selected media type so it is restored on next launch."""
        val = self._browser._media_combo.currentData()
        try:
            from tool import prefs as _prefs
            _prefs.set("ill_media_type", val if val is not None else "")
        except Exception:
            pass

    

    def _build_mode_filter_section(
        self, panel: TabPanel, browser: "IllustrationBrowser"
    ) -> None:
        """Add a Mode collapsible section with Isolated+Frame / Frame / Isolated combo."""
        mode_sec = CollapsibleSection("Mode", pref_key="ill_eng_section_mode")
        combo = QComboBox()
        combo.setFocusPolicy(Qt.NoFocus)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContentsOnFirstShow)
        combo.addItem("Isolated + Frame", userData="")          # no filter
        combo.addItem("Frame",            userData="frame")
        combo.addItem("Isolated",         userData="isolated")
        combo.setStyleSheet(
            f"QComboBox {{ background: {theme.BTN_BG}; color: {theme.TEXT};"
            f" border: none; border-radius: 3px; padding: 0px 6px;"
            f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
            f" font-weight: bold;"
            f" min-height: 24px; max-height: 24px; }}"
            f"QComboBox::drop-down {{ border: none; }}"
        )

        # Ensure popup uses canonical styling/cleanup
        attach_combo_popup(combo)

        def _on_mode_changed(_idx: int) -> None:
            mode = combo.currentData()  # "" = Both (no filter), "frame", "isolated"
            self._eng_source.set_mode_filter(mode)
            browser.refresh()

        combo.currentIndexChanged.connect(_on_mode_changed)
        mode_sec.add_widget(combo)
        panel.add_widget(mode_sec)

    def _build_engraving_tools_section(self, panel: TabPanel) -> None:
        """Add the Engravings Tools section (Viewer + Best buttons)."""
        tools_sec = CollapsibleSection("Tools", pref_key="ill_eng_section_tools")
        _icon_sz = QSize(14, 14)
        _abtn = self._btn_style()
        _v_icon, _v_icon_hover = self._make_btn_icon("media-image", 14)
        _s_icon, _s_icon_hover = self._make_btn_icon("media-image", 14)
        _row = QWidget()
        _rl  = QHBoxLayout(_row)
        _rl.setContentsMargins(0, 0, 0, 0)
        _rl.setSpacing(theme.SECTION_GAP)

        self._eng_best_btn = QPushButton("Best")
        self._eng_best_btn.setCheckable(True)
        self._eng_best_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_best_btn.setEnabled(False)
        self._eng_best_btn.setFixedHeight(theme.BTN_H)
        self._eng_best_btn.setStyleSheet(_abtn + (
            f"QPushButton:checked {{ background-color: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
        ))
        self._eng_best_btn.clicked.connect(self._on_best_btn_clicked)
        tools_sec.add_widget(self._eng_best_btn)

        self._eng_view_btn = HoverIconButton("Viewer", _v_icon, _v_icon_hover)
        self._eng_view_btn.setIconSize(_icon_sz)
        self._eng_view_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_view_btn.setEnabled(False)
        self._eng_view_btn.setStyleSheet(_abtn)
        self._eng_view_btn.clicked.connect(self._open_engraving_in_viewer)
        _rl.addWidget(self._eng_view_btn, 1)
        self._eng_sil_btn = HoverIconButton("Silhouette", _s_icon, _s_icon_hover)
        self._eng_sil_btn.setIconSize(_icon_sz)
        self._eng_sil_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_sil_btn.setEnabled(False)
        self._eng_sil_btn.setStyleSheet(_abtn)
        self._eng_sil_btn.clicked.connect(self._jump_to_silhouette)
        _rl.addWidget(self._eng_sil_btn, 1)
        tools_sec.add_widget(_row)

        # Delete button — removes the engraving directory; Del/Backspace also fires it
        _del_icon, _del_icon_hover = self._make_btn_icon("trash", 14)
        self._eng_delete_btn = HoverIconButton("Delete", _del_icon, _del_icon_hover)
        self._eng_delete_btn.setIconSize(_icon_sz)
        self._eng_delete_btn.setFocusPolicy(Qt.NoFocus)
        self._eng_delete_btn.setEnabled(False)
        self._eng_delete_btn.setStyleSheet(_abtn)
        self._eng_delete_btn.clicked.connect(self._delete_engraving)
        tools_sec.add_widget(self._eng_delete_btn)
        self._eng_rebuild_index_btn = self._make_rebuild_index_button()
        tools_sec.add_widget(self._eng_rebuild_index_btn)
        panel.add_widget(tools_sec)

    def _make_rebuild_index_button(self) -> QPushButton:
        button = QPushButton("Rebuild Index")
        button.setFocusPolicy(Qt.NoFocus)
        button.setFixedHeight(theme.BTN_H)
        button.setStyleSheet(self._btn_style())
        button.clicked.connect(self._start_index_rebuild)
        return button

    def _start_index_rebuild(self) -> None:
        """Rebuild both source indexes for the currently selected media type."""
        if getattr(self, "_index_worker", None) is not None:
            return
        media_type = self._browser._media_type
        if not media_type:
            return
        for button in (self._sil_rebuild_index_btn, self._eng_rebuild_index_btn):
            button.setEnabled(False)
            button.setText("Rebuilding...")
        for browser in (self._browser_sil, self._browser_eng):
            browser._loading_bar.start()
            browser._loading_timer.start()
        self._index_worker = _IllustrationIndexWorker(
            self._project_path, media_type, parent=self
        )
        self._index_worker.finished.connect(self._on_index_rebuild_finished)
        self._index_worker.start()

    def _on_index_rebuild_finished(self, ok: bool, error: str) -> None:
        self._index_worker = None
        for button in (self._sil_rebuild_index_btn, self._eng_rebuild_index_btn):
            button.setEnabled(True)
            button.setText("Rebuild Index" if ok else "Rebuild Failed")
            button.setToolTip(error if not ok else "")
        if ok:
            inactive = (
                self._browser_eng
                if self._browser is self._browser_sil
                else self._browser_sil
            )
            inactive._stop_catalog_loader()
            inactive._load_requested = False
            inactive._loading_timer.stop()
            inactive._loading_bar.stop()
            self._browser.reload()
            return
        for browser in (self._browser_sil, self._browser_eng):
            browser._loading_timer.stop()
            browser._loading_bar.stop()

    # ------------------------------------------------------------------
    # Sort controls

    def _on_sort_changed(self, _idx: int) -> None:
        """Update the source sort order and refresh the active browser."""
        # Only respond to the active tab's combo to avoid double-firing.
        if self.sender() is not self._sort_combo:
            return
        key  = self._sort_combo.currentData()
        keys = [key] if key else []
        source = self._browser._source
        if hasattr(source, "set_sort_keys"):
            source.set_sort_keys(keys)
        self._browser.reload()

    # ------------------------------------------------------------------
    # Selection

    def _on_selection_changed(self, rec: dict) -> None:
        """Update the inspector panel when the browser selection changes."""
        self._show_object_meta(rec)
        self._update_best_btn()

    # ------------------------------------------------------------------
    # Object inspector

    def _clear_meta(self) -> None:
        for lbl in self._meta_rows.values():
            lbl.setText("—")
        self._current_rec = None
        if hasattr(self, "_eng_view_btn"):
            self._eng_view_btn.setEnabled(False)
        if hasattr(self, "_eng_sil_btn"):
            self._eng_sil_btn.setEnabled(False)
        if hasattr(self, "_eng_delete_btn"):
            self._eng_delete_btn.setEnabled(False)
            self._eng_delete_btn.setText("Delete")
        btn = self._active_best_btn
        if btn is not None:
            btn.setEnabled(False)
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        if hasattr(self, "_shotlist_btn"):
            self._shotlist_btn.setEnabled(False)
            self._sam_btn.setEnabled(False)
        if hasattr(self, "_eng_viz_btn"):
            self._eng_viz_btn.setEnabled(False)
        self._update_eng_buttons()

    def _activate_primary_action(self) -> None:
        """Shift+Enter / double-click primary action for the active source."""
        if self._browser is self._browser_eng:
            self._open_engraving_in_viewer()
        else:
            self._open_in_shotlist()

    def _show_object_meta(self, rec: dict) -> None:
        def _set(key: str, value: str) -> None:
            lbl = self._meta_rows.get(key)
            if lbl is not None:
                lbl.setText(value)

        def _stored(key):
            v = rec.get(f"{key}_score")
            if v is None:
                v = rec.get(key)
            try:
                return float(v) if v is not None else None
            except Exception:
                return None

        def _fmt(v):
            return f"{v:.3f}" if v is not None else "—"

        # Values below are handed to InspectorValue as-is (via _set/_meta_rows,
        # which are backed by MetadataBlock -> InspectorValue). InspectorValue
        # applies its own character wrap for unbroken strings (filenames, ids)
        # so no local truncation/clipping is needed here.
        film = rec.get("filename_stem") or rec.get("filename") or "—"
        shot_id = str(rec.get("shot_id", "—"))

        # Keys shared by all sources
        _set("label", rec.get("label", "—"))
        _set("film",  film)
        _set("model", rec.get("sam_model") or rec.get("model") or "—")

        # Silhouette-only keys
        _set("shot",  shot_id)
        _set("frame", str(rec.get("frame", "—")))
        _set("confidence", _fmt(_stored("confidence")))

        # size: derive from mask_area + frame_size when not yet scored
        size_val = _stored("size")
        if size_val is None:
            mask_area  = rec.get("mask_area")
            frame_size = rec.get("frame_size") or []
            if mask_area is not None and len(frame_size) >= 2:
                frame_area = float(max(1, frame_size[0] * frame_size[1]))
                area_frac  = float(mask_area) / frame_area
                size_val   = max(0.0, min(1.0, (area_frac - 0.002) / max(1e-9, 0.298)))

        # fullness: derive from mask_area + bbox when not yet scored
        fullness_val = _stored("fullness")
        if fullness_val is None:
            mask_area = rec.get("mask_area")
            bbox = rec.get("bbox") or []
            if mask_area is not None and len(bbox) >= 4:
                bbox_area    = float(max(1, bbox[2] * bbox[3]))
                fullness_val = max(0.0, min(1.0, float(mask_area) / bbox_area))

        _set("usefulness",     _fmt(_stored("usefulness")))
        _set("fullness",       _fmt(fullness_val))
        _set("size",           _fmt(size_val))
        _set("overlap",        _fmt(_stored("overlap")))
        _set("semantic_label", _fmt(_stored("semantic_label")))
        _set("semantic_field", _fmt(_stored("semantic_field")))

        # Engraving-only keys
        _set("mode",      rec.get("mode", "—"))
        _set("object_id", str(rec.get("object_id", "—")))

        # Engraving tools buttons
        if hasattr(self, "_eng_view_btn"):
            has_png = bool(rec.get("output_png") or rec.get("raw_png"))
            self._eng_view_btn.setEnabled(has_png)
        if hasattr(self, "_eng_sil_btn"):
            self._eng_sil_btn.setEnabled(
                bool(rec.get("label") or rec.get("filename_stem"))
            )
        if hasattr(self, "_eng_delete_btn"):
            has_dir = bool(rec.get("path") and Path(str(rec.get("path"))).parent.is_dir())
            self._eng_delete_btn.setEnabled(has_dir)
            self._eng_delete_btn.setText("Delete")

        self._current_rec = rec
        _can_open = bool(rec.get("filename") and rec.get("shot_id"))
        if hasattr(self, "_shotlist_btn"):
            self._shotlist_btn.setEnabled(_can_open)
            self._sam_btn.setEnabled(_can_open)
        self._update_eng_buttons()

    # ------------------------------------------------------------------
    # Best-selection workflow

    def _update_best_btn(self) -> None:
        btn = self._active_best_btn
        if btn is None:
            return
        rec = self._browser.currentItem()
        if rec is None:
            btn.setEnabled(False)
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
            return
        btn.setEnabled(True)
        btn.blockSignals(True)
        btn.setChecked(bool(rec.get("human_best")))
        btn.blockSignals(False)

    def _on_best_btn_clicked(self, checked: bool) -> None:
        if checked:
            self._mark_best()
        else:
            self._unmark_best()

    def _toggle_best(self) -> None:
        rec = self._browser.currentItem()
        if rec is None:
            return
        if rec.get("human_best"):
            self._unmark_best()
        else:
            self._mark_best()

    def _mark_best(self) -> None:
        from services.silhouette_curation import mark_best
        rec = self._browser.currentItem()
        if rec is None:
            return
        # Collect all records for the same label (from the filtered list)
        label = rec.get("label", "")
        all_label_recs = [
            r for r in self._browser._filtered_items
            if r.get("label") == label
        ]
        mark_best(rec, all_label_recs)
        self._browser.refresh_highlights()
        self._update_best_btn()

    def _unmark_best(self) -> None:
        from services.silhouette_curation import unmark_best
        rec = self._browser.currentItem()
        if rec is None:
            return
        unmark_best(rec)
        self._browser.refresh_highlights()
        self._update_best_btn()

    # ------------------------------------------------------------------
    # Action buttons

    def _count_pending_engravings(self) -> int:
        """Count marked silhouettes whose engraving is not yet generated."""
        try:
            from services.engraving_paths import engraving_status as _eng_status
            return sum(
                1 for r in (self._sil_source.items() if hasattr(self, "_sil_source") else [])
                if r.get("human_best") and r.get("path") and
                _eng_status(self._project_path, r["path"], r) != "generated"
            )
        except Exception:
            return 0

    def _update_eng_buttons(self) -> None:
        """Enable/disable and label the Engraving and Marked generation buttons."""
        if not hasattr(self, "_eng_gen_btn"):
            return
        if self._eng_gen_running or self._eng_batch_running:
            return  # keep running state intact

        rec = self._current_rec
        if rec and rec.get("path"):
            status = None
            try:
                from services.engraving_paths import engraving_status as _eng_status
                status = _eng_status(self._project_path, rec["path"], rec)
                done = status in ("generated", "generating")
            except Exception:
                done = False
            self._eng_gen_btn.setEnabled(not done)
            self._eng_gen_btn.setChecked(False)
            self._eng_gen_btn.setText("Engrave")
            self._eng_viz_btn.setEnabled(status == "generated")
        else:
            self._eng_gen_btn.setEnabled(False)
            self._eng_gen_btn.setChecked(False)
            self._eng_gen_btn.setText("Engrave")
            self._eng_viz_btn.setEnabled(False)

        n = self._count_pending_engravings()
        self._eng_batch_btn.setEnabled(n > 0)
        self._eng_batch_btn.setChecked(False)
        self._eng_batch_btn.setText(f"Generate Marked ({n})" if n > 0 else "Generate Marked")

    def _refresh_batch_count(self) -> None:
        """Re-check pending count while batch is running and update button label."""
        if not self._eng_batch_running:
            self._eng_count_timer.stop()
            return
        n = self._count_pending_engravings()
        self._eng_batch_btn.setText(f"Cancel  ({n} left)")

    def _tick_eng_animation(self) -> None:
        """Animate the single-item Engraving button while it runs."""
        self._eng_anim_dots = (self._eng_anim_dots + 1) % 4
        dots = "." * max(1, self._eng_anim_dots)
        if self._eng_gen_running:
            self._eng_gen_btn.setText(f"Generating{dots}")
        if not self._eng_gen_running:
            self._eng_anim_timer.stop()

    def _start_engraving_generation(self) -> None:
        """Generate an engraving for the currently selected silhouette via OpenAI."""
        rec = self._current_rec
        if not rec or not rec.get("path"):
            return
        self._eng_gen_running = True
        self._eng_gen_btn.setChecked(True)   # ACCENT highlight while running
        self._eng_gen_btn.setEnabled(False)
        self._eng_batch_btn.setEnabled(False)
        self._eng_anim_dots = 0
        self._eng_anim_timer.start()
        cmd = [
            sys.executable,
            str(Path(__file__).parent.parent / "cli.py"),
            "engraving", "generate",
            "--source", str(rec["path"]),
            "--provider", "openai",
        ]
        self._eng_gen_worker = _EngravingWorker(cmd, parent=self)
        self._eng_gen_worker.finished.connect(self._on_eng_gen_finished)
        self._eng_gen_worker.start()

    def _on_eng_gen_finished(self, ok: bool, err: str) -> None:
        self._eng_gen_running = False
        self._eng_gen_worker  = None
        self._eng_gen_btn.setChecked(False)
        if not ok:
            self._eng_gen_btn.setText("Failed")
            if err:
                self._eng_gen_btn.setToolTip(err)
            QTimer.singleShot(4000, lambda: (
                self._eng_gen_btn.setText("Engrave"),
                self._eng_gen_btn.setToolTip(""),
            ))
        # Full reload so the new engraving.json is picked up from disk.
        self._browser_eng.reload()
        self._update_eng_buttons()

    def _toggle_batch_generation(self) -> None:
        """Start batch generation, or cancel it if already running."""
        if self._eng_batch_running:
            self._cancel_batch_generation()
        else:
            self._start_batch_generation()

    def _start_batch_generation(self) -> None:
        """Generate engravings for all marked silhouettes that need them."""
        n = self._count_pending_engravings()
        if n == 0:
            return
        self._eng_batch_running = True
        self._eng_gen_btn.setEnabled(False)
        self._eng_batch_btn.setEnabled(True)   # keep enabled so user can cancel
        self._eng_batch_btn.setChecked(True)   # ACCENT highlight
        self._eng_batch_btn.setText(f"Cancel  ({n} left)")
        self._eng_count_timer.start()
        media = self._browser_sil._media_type or "movie"
        cmd = [
            sys.executable,
            str(Path(__file__).parent.parent / "cli.py"),
            "engraving", "batch",
            "--media", media,
            "--verbose",
        ]
        self._eng_batch_worker = _BatchEngravingWorker(cmd, parent=self)
        self._eng_batch_worker.finished.connect(self._on_eng_batch_finished)
        self._eng_batch_worker.start()

    def _cancel_batch_generation(self) -> None:
        """Cancel a running batch generation."""
        if self._eng_batch_worker is not None:
            self._eng_batch_worker.cancel()
        # State reset happens in _on_eng_batch_finished(False, "Cancelled")

    def _on_eng_batch_finished(self, ok: bool, err: str) -> None:
        self._eng_batch_running = False
        self._eng_batch_worker  = None
        self._eng_count_timer.stop()
        self._eng_batch_btn.setChecked(False)
        if not ok and err and err != "Cancelled":
            self._eng_batch_btn.setText("Failed")
            if err:
                self._eng_batch_btn.setToolTip(err)
            QTimer.singleShot(4000, lambda: (
                self._eng_batch_btn.setToolTip(""),
                self._update_eng_buttons(),
            ))
        else:
            self._update_eng_buttons()
        self._browser_eng.refresh()

    def _open_engraving_in_viewer(self) -> None:
        """Open the selected engraving's output PNG in the OS default image viewer."""
        rec = self._current_rec
        if not rec:
            return
        for key in ("output_png", "raw_png"):
            path = rec.get(key, "")
            if path and Path(path).exists():
                from PyQt5.QtCore import QUrl
                from PyQt5.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
                return

    def _jump_to_silhouette(self) -> None:
        """Switch to the Silhouettes tab and navigate to the source silhouette."""
        rec = self._current_rec
        if not rec:
            return
        from visualizers.components.illustration_browser import _clean_stem
        filename_stem = _clean_stem(rec.get("filename_stem") or "")
        label         = rec.get("label") or ""

        self._side_scroll.setCurrentIndex(0)

        # Clear the grid immediately so the user sees empty state
        # rather than stale previous content during the navigation delay.
        self._browser_sil._filtered_items = []
        self._browser_sil._selected_index = -1
        self._browser_sil._page_index     = 0
        self._browser_sil._rebuild_grid()
        self._browser_sil._loading_bar.start()
        self._browser_sil._loading_timer.start()

        object_id = rec.get("object_id") or ""

        def _navigate() -> None:
            self._browser_sil._loading_timer.stop()
            self._browser_sil._loading_bar.stop()
            self._browser_sil.navigate_direct(
                item      = filename_stem or None,
                keyword   = label or None,
                object_id = object_id or None,
            )

        already_loaded = any(
            _clean_stem(r.get("filename_stem") or "") == filename_stem
            for r in self._browser_sil._all_items
        )

        if already_loaded:
            QTimer.singleShot(150, _navigate)
        else:
            def _after_reload() -> None:
                try:
                    self._browser_sil.catalogReloaded.disconnect(_after_reload)
                except Exception:
                    pass
                QTimer.singleShot(150, _navigate)

            self._browser_sil.catalogReloaded.connect(_after_reload)
            self._browser_sil.reload()

    def _visualize_engraving(self) -> None:
        """Switch to the Engravings tab and navigate to the current silhouette's engraving.

        If the engraving hasn't been loaded into the browser cache yet (e.g. just
        generated), reloads the source first and navigates once loading completes.
        """
        rec = self._current_rec
        if not rec:
            return

        from visualizers.components.illustration_browser import _clean_stem
        filename_stem = _clean_stem(rec.get("filename_stem") or "")
        # Use the silhouette's label directory name as the engraving keyword.
        # Engraving records store the normalised directory name ("a_t__s_f__sign")
        # not the real label ("A.T.&S.F. sign"), so path.parent.name is the match.
        sil_path  = Path(str(rec.get("path", "")))
        eng_label = sil_path.parent.name  # label directory = engraving record label

        # Clear grid + start loading animation immediately on tab switch.
        self._browser_eng._filtered_items = []
        self._browser_eng._selected_index = -1
        self._browser_eng._page_index     = 0
        self._browser_eng._rebuild_grid()
        self._browser_eng._loading_bar.start()
        self._browser_eng._loading_timer.start()

        def _navigate() -> None:
            self._browser_eng._loading_timer.stop()
            self._browser_eng._loading_bar.stop()
            self._browser_eng.navigate_direct(
                item    = filename_stem or None,
                keyword = eng_label or None,
            )

        # Switch to Engravings tab (triggers _on_source_tab_changed).
        self._side_scroll.setCurrentIndex(1)

        # Check whether the target is already in the browser's cached items.
        already_loaded = any(
            _clean_stem(r.get("filename_stem") or "") == filename_stem
            and r.get("label") == eng_label
            for r in self._browser_eng._all_items
        )

        if already_loaded:
            QTimer.singleShot(150, _navigate)
        else:
            # Target not cached yet — reload and navigate once the scan finishes.
            def _after_reload() -> None:
                try:
                    self._browser_eng.catalogReloaded.disconnect(_after_reload)
                except Exception:
                    pass
                QTimer.singleShot(150, _navigate)

            self._browser_eng.catalogReloaded.connect(_after_reload)
            self._browser_eng.reload()

    def _delete_engraving(self) -> None:
        """Delete the selected engraving's mode directory, then refresh the browser.

        The deletion runs in a background thread so the UI stays responsive.
        The engraving directory (``<mode>/``) is removed; parent directories
        (``<object_id>/``, ``<label>/``, ``<film>/``) are left in place so
        sibling engravings are not affected.
        """
        if self._browser is not self._browser_eng:
            return
        rec = self._current_rec
        if not rec:
            return
        eng_json = rec.get("path")
        if not eng_json:
            return
        mode_dir = Path(str(eng_json)).parent
        if not mode_dir.is_dir():
            return
        # Disable the button while deleting to prevent double-fire.
        self._eng_delete_btn.setEnabled(False)
        self._eng_delete_btn.setText("Deleting…")
        self._delete_worker = _DeleteWorker(mode_dir, parent=self)
        self._delete_worker.finished.connect(self._on_delete_finished)
        self._delete_worker.start()

    def _on_delete_finished(self) -> None:
        """Called when the background delete completes; clears state and reloads."""
        self._delete_worker = None
        try:
            from services.illustration_index import invalidate_index
            invalidate_index(
                self._project_path,
                "engravings",
                self._browser_eng._media_type or "movie",
            )
        except Exception:
            pass
        self._clear_meta()
        self._browser_eng.reload()

    def _open_in_shotlist(self) -> None:
        rec = self._current_rec
        if not rec:
            return
        filename = rec.get("filename") or ""
        shot_id  = str(rec.get("shot_id") or "")
        if not filename:
            return
        from visualizers.shot_visualizer import open_at_shot
        open_at_shot(self._project_path, filename,
                     self._browser._media_type or "movie", shot_id=shot_id,
                     loop=True, no_continue=True, play=True)

    def _open_sam_explorer(self) -> None:
        rec = self._current_rec
        if not rec:
            return
        from tool import prefs as _prefs
        from visualizers.segmentation_visualizer import SAMExplorer
        model_name = _prefs.get("model_segmentation", _DEFAULT_MODEL) or _DEFAULT_MODEL
        self._sam_explorer_win = SAMExplorer(
            self._project_path,
            media_type=self._browser._media_type,
            model_name=model_name,
        )
        self._sam_explorer_win.show()
        filename = rec.get("filename") or ""
        shot_id  = str(rec.get("shot_id") or "")
        concept  = rec.get("label") or ""
        if filename and shot_id:
            self._sam_explorer_win.navigate_to(filename, shot_id, concept=concept)

    # ------------------------------------------------------------------
    # Keyboard handling

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()
        if not (mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)):
            ch = event.text().upper()
            if len(ch) == 1 and (ch.isalpha() or ch == "#"):
                self._handle_letter_key(ch)
                return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if mod & Qt.ShiftModifier:
                self._open_in_shotlist()
            else:
                self._toggle_best()
            return
        if key in (Qt.Key_Home, Qt.Key_End,
                   Qt.Key_PageUp, Qt.Key_PageDown,
                   Qt.Key_Up, Qt.Key_Down,
                   Qt.Key_Left, Qt.Key_Right):
            self._handle_nav_key(key, mod)
        else:
            super().keyPressEvent(event)

    def _handle_nav_key(self, key: int, mod) -> None:
        if key == Qt.Key_Home:
            self._browser.stepItem(-1)
        elif key == Qt.Key_End:
            self._browser.stepItem(1)
        elif key == Qt.Key_PageUp:
            self._browser.stepField(-1)
        elif key == Qt.Key_PageDown:
            self._browser.stepField(1)
        elif key == Qt.Key_Up:
            if mod & Qt.ShiftModifier:
                self._browser.stepKeyword(-1)
            else:
                self._browser.navigate_grid(0, -1)
        elif key == Qt.Key_Down:
            if mod & Qt.ShiftModifier:
                self._browser.stepKeyword(1)
            else:
                self._browser.navigate_grid(0, 1)
        elif key == Qt.Key_Left:
            if mod & Qt.ShiftModifier:
                self._browser._on_prev_page()
            else:
                self._browser.navigate_grid(-1, 0)
        elif key == Qt.Key_Right:
            if mod & Qt.ShiftModifier:
                self._browser._on_next_page()
            else:
                self._browser.navigate_grid(1, 0)

    def _handle_letter_key(self, letter: str) -> None:
        """Toggle the alphabetical bucket filter for *letter*.

        First press → activates the letter filter.
        Second press of the same letter → resets to <Letter> (no filter).
        """
        current = self._browser._letter_combo.currentData()
        if current == letter:
            # Already on this letter — toggle off
            self._browser.navigate_to_filters(letter="--all")
        else:
            self._browser.navigate_to_filters(letter=letter)

    # ------------------------------------------------------------------
    # IPC navigation

    def navigate_to(
        self,
        film: Optional[str] = None,
        field: Optional[str] = None,
        label: Optional[str] = None,
        shot_id: Optional[str] = None,
    ) -> None:
        """Select *field*, *film*, and *label* in the filter combos.

        If *shot_id* is given the matching thumbnail is also selected.
        """
        self._browser.navigate_to_filters(
            item=film,
            field=field,
            keyword=label,
        )
        if shot_id:
            for abs_idx, rec in enumerate(self._browser._filtered_items):
                if str(rec.get("shot_id", "")) == str(shot_id):
                    self._browser.select_index(abs_idx)
                    break


# Main window
# ---------------------------------------------------------------------------

class IllustrationWindow(WindowVisualizer):
    """Top-level host for the framework reference visualizer.

    Owns window shell concerns (geometry, fullscreen state, IPC lifecycle) and
    delegates browsing/presentation responsibilities to ``IllustrationPane``.
    """

    def __init__(
        self,
        project_path: str,
        media_type: Optional[str] = None,
        model_name: str = _DEFAULT_MODEL,
        initial_film: Optional[str] = None,
        initial_field: Optional[str] = None,
        initial_label: Optional[str] = None,
        initial_shot: Optional[str] = None,
    ) -> None:
        # Initialize attributes used by create_browser/create_inspector
        # before WindowVisualizer.__init__ runs (it calls those hooks).
        self._project_path = project_path
        self._media_type = media_type
        self._model_name = model_name
        self._initial_film = initial_film
        self._initial_field = initial_field
        self._initial_label = initial_label
        self._initial_shot = initial_shot

        # Let WindowVisualizer manage geometry persistence for this window.
        super().__init__(pref_key="window_illustration")
        self.setWindowTitle("Illustration")

        # IllustrationPane is the visual catalog; create_browser will have
        # already instantiated it and returned its browser widget. Configure
        # the window sizing and restore additional state after layout.
        self.setMinimumSize(900, 560)
        self.resize(1300, 760)
        QTimer.singleShot(0, self._restore_saved_state)

        # (shared gutter helper will be attached by WindowVisualizer)

        # IPC server — lets open_at_illustration navigate an existing instance
        self._ipc_server = _IllIpcServer(project_path, parent=self)
        self._ipc_server.navigate_requested.connect(self._on_ipc_navigate)
        self._ipc_server.start()

    def _on_ipc_navigate(
            self, film: str, field: str, label: str, shot_id: str, media_type: str
    ) -> None:
            """Raise this window, switch media type if requested, then navigate."""
            # Preserve fullscreen state instead of unconditionally calling
            # showNormal() — this window lives in its own OS process, so
            # this IPC-triggered raise is the cross-process equivalent of
            # `raise_existing_window()`'s in-process raise, which uses this
            # same fullscreen-preserving pattern.
            was_fullscreen = self.isFullScreen()
            if was_fullscreen:
                self.showFullScreen()
            else:
                self.show()
                self.showNormal()
            self.raise_()
            self.activateWindow()

            # If a media_type was requested and it differs from the current
            # catalog mode, update the UI controls first so the browser reloads
            # into the requested mode before navigation.
            try:
                if media_type:
                    cat = getattr(self, '_catalog', None)
                    if cat is not None and getattr(cat, '_media_type', None) != media_type:
                        # Update the pane-level control (if present)
                        try:
                            if getattr(cat, '_media_type_combo', None) is not None:
                                cat._media_type_combo.setCurrentText(media_type)
                        except Exception:
                            pass
                        # Update each browser's internal media combo to trigger reload
                        try:
                            for b in (getattr(cat, '_browser_sil', None), getattr(cat, '_browser_eng', None)):
                                if b is None:
                                    continue
                                try:
                                    idx = b._media_combo.findData(media_type)
                                    if idx >= 0:
                                        b._media_combo.setCurrentIndex(idx)
                                except Exception:
                                    pass
                        except Exception:
                            pass
            except Exception:
                pass

            # Perform navigation (may be applied immediately or deferred by the
            # browser while it reloads its items). Ensure navigation runs after
            # the requested mode has been requested above.
            self._catalog.navigate_to(
                film or None,
                field or None,
                label or None,
                shot_id or None,
            )

    # ------------------------------------------------------------------
    # WindowVisualizer hooks
    def create_browser(self) -> QWidget:
        # Instantiate the IllustrationPane (catalog) and return its browser widget.
        self._catalog = IllustrationPane(self._project_path, media_type=self._media_type)
        if self._initial_film or self._initial_field or self._initial_label or self._initial_shot:
            QTimer.singleShot(0, lambda: self._catalog.navigate_to(
                self._initial_film, self._initial_field, self._initial_label, self._initial_shot
            ))
        return self._catalog._browser_stack

    def create_inspector(self) -> QWidget:
        # Return the side inspector/filter pane created by the catalog.
        return getattr(self, "_catalog", None)._side_scroll

    def _fit_splitter_width(self) -> None:
        """Same shape as `WindowVisualizer._fit_splitter_width()`, but with
        Illustration's inspector floor matched to Metadata's effective
        minimum width (`_INSPECTOR_MIN_W`) instead of the base class's
        generic 320px floor.

        `WindowVisualizer` itself cannot be changed (its floor is a shared
        default used by other visualizers too), so this override stays
        local to Illustration. The content-driven growth behavior (still
        using `sizeHint().width()` when it legitimately needs more than the
        floor) is preserved unchanged — only the floor constant differs.
        """
        total = self._splitter.width()
        if total <= 0:
            QTimer.singleShot(100, self._fit_splitter_width)
            return
        try:
            inspector_w = max(_INSPECTOR_MIN_W, self._inspector_shell.sizeHint().width())
        except Exception:
            inspector_w = _INSPECTOR_MIN_W
        self._inspector_shell.setMinimumWidth(inspector_w)
        self._splitter.setSizes([max(1, total - inspector_w), inspector_w])

    def _restore_saved_state(self) -> None:
        """Restore inspector visibility, splitter sizes and fullscreen state
        from prefs.

        Deferred to a single-shot timer so it runs after the initial layout
        pass (`WindowVisualizer._fit_splitter_width`) has already set
        default pane widths.
        """
        from tool import prefs as _prefs

        # ── Splitter sizes ───────────────────────────────────────────────────
        saved_sizes = _prefs.get("window_illustration_panel_sizes")
        if saved_sizes and len(saved_sizes) == 2:
            # Clamp the restored inspector width instead of trusting the
            # saved value verbatim. A stale value (e.g. saved by a
            # since-fixed startup-width bug, or saved from a wider
            # window/screen than this one) must not be able to reopen the
            # inspector wider than is sane for the CURRENT window — floor
            # is the pane's own content-driven minimum (same floor
            # `_fit_splitter_width()` already uses), ceiling is half of
            # the current window width (the inspector is a side panel and
            # should never out-grow the browser it's paired with). This is
            # local to Illustration only; it does not touch how the
            # splitter itself behaves, only which numbers get fed to it.
            inspector_w = int(saved_sizes[1])
            floor_w = max(_INSPECTOR_MIN_W, self._inspector_shell.sizeHint().width())
            ceiling_w = max(floor_w, self.width() // 2)
            inspector_w = min(max(inspector_w, floor_w), ceiling_w)
            browser_w = max(1, self.width() - inspector_w)
            self._saved_splitter_sizes = [browser_w, inspector_w]

        # ── Mode (BROWSER / TOOLS) ───────────────────────────────────────────
        in_browser_mode = bool(_prefs.get("window_illustration_browser_mode"))
        if in_browser_mode:
            self._inspector_hidden = True
            self._inspector_shell.setVisible(False)
            # Browser fills the window; no setSizes needed
        else:
            if self._saved_splitter_sizes:
                self._splitter.setSizes(self._saved_splitter_sizes)
            else:
                self._fit_splitter_width()

        # ── Fullscreen ───────────────────────────────────────────────────────
        if _prefs.get("window_illustration_fullscreen"):
            self.showFullScreen()

    def closeEvent(self, event) -> None:
        from tool import prefs as _prefs
        _prefs.set("window_illustration_fullscreen", self.isFullScreen())
        _prefs.set("window_illustration_browser_mode", self._inspector_hidden)
        # Save the TOOLS-mode splitter sizes. When the inspector is hidden
        # the splitter sizes are meaningless (inspector pane is invisible),
        # so save the pre-hide sizes that _toggle_inspector stored in
        # _saved_splitter_sizes instead.
        if self._inspector_hidden and self._saved_splitter_sizes:
            panel_sizes = self._saved_splitter_sizes
        else:
            panel_sizes = list(self._splitter.sizes())
        _prefs.set("window_illustration_panel_sizes", panel_sizes)
        self._ipc_server.stop()
        self._ipc_server.wait(1000)
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()
        # Delegate global window shortcuts to WindowVisualizer
        if key == Qt.Key_Escape or (key in (Qt.Key_Q, Qt.Key_W) and mod & Qt.ControlModifier):
            super().keyPressEvent(event)
            return
        if key in (Qt.Key_Backtab, Qt.Key_Tab) and mod & Qt.ShiftModifier:
            # Shift+Tab — WindowVisualizer toggles fullscreen
            super().keyPressEvent(event)
            return
        if key == Qt.Key_Tab and not (mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)):
            # Tab — WindowVisualizer owns inspector/panel toggling
            super().keyPressEvent(event)
            return
        elif key in (Qt.Key_Home, Qt.Key_End,
                     Qt.Key_PageUp, Qt.Key_PageDown,
                     Qt.Key_Up, Qt.Key_Down,
                     Qt.Key_Left, Qt.Key_Right):
            self._catalog._handle_nav_key(key, mod)
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            if mod & Qt.ShiftModifier:
                self._catalog._activate_primary_action()
            else:
                self._catalog._toggle_best()
        elif key in (Qt.Key_Delete, Qt.Key_Backspace) and not (
            mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)
        ):
            self._catalog._delete_engraving()
        elif not (mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)):
            ch = event.text().upper()
            if len(ch) == 1 and (ch.isalpha() or ch == "#"):
                self._catalog._handle_letter_key(ch)
        else:
            super().keyPressEvent(event)



## `run_visualizer` removed — use `visualizers.launcher.launch_visualizer`


def open_at_illustration(
    project_path: str,
    filename_stem: str = "",
    field: Optional[str] = None,
    media_type: str = "movie",
    label: Optional[str] = None,
    shot_id: Optional[str] = None,
) -> None:
    """Open (or navigate) the Illustration Visualizer.

    If an instance is already running (socket exists), delivers a navigate
    command via IPC and raises the existing window.  Otherwise spawns a new
    process with the supplied filter arguments.
    """
    # Try IPC first (works whether the window is in-process or a subprocess)
    if _ill_ipc_send_navigate(
        project_path,
        film=filename_stem,
        field=field or "",
        label=label or "",
        shot_id=str(shot_id) if shot_id else "",
        media_type=media_type or "",
    ):
        # Also raise any in-process window
        from visualizers._window_helpers import raise_existing_window
        raise_existing_window("illustration")
        return

    # No IPC endpoint responded — delegate process creation to the canonical
    # launcher. The launcher is the single place that knows how to start an
    # independent Illustration process; it will either create a QApplication
    # and show a window (CLI / standalone case) or spawn a new OS process
    # when invoked from inside an existing visualizer process.
    from visualizers.launcher import launch_visualizer
    launch_visualizer(
        "illustration",
        project_path,
        media_type=media_type,
        initial_film=filename_stem,
        initial_field=field,
        initial_label=label,
        initial_shot=shot_id,
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Illustration Visualizer")
    ap.add_argument("--project", required=True, help="Project path")
    ap.add_argument("--media",  default="movie")
    ap.add_argument("--field",  default=None)
    ap.add_argument("--film",   default=None, help="Initial film stem to select")
    ap.add_argument("--label",  default=None, help="Initial label to select")
    ap.add_argument("--shot",   default=None, help="Initial shot_id to select")
    parsed = ap.parse_args()
    from visualizers.launcher import launch_visualizer
    launch_visualizer(
        "illustration",
        parsed.project,
        media_type=parsed.media,
        initial_film=parsed.film,
        initial_field=parsed.field,
        initial_label=parsed.label,
        initial_shot=parsed.shot,
    )
