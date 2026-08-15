#!/usr/bin/env python3
"""Project Visualizer — launcher and configuration hub for Crossing Tool.

Opened via:
    crossing visualizer project
    crossing visualizer          (no subcommand — default)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.combo_popup import style_canonical_combo
from visualizers.components.inspector import Inspector
from visualizers.components.metadata_block import INSPECTOR_ROW_HEIGHT, table_key_cell_style
from visualizers.components.sweep_bar import SweepBar
from visualizers.components.tab_panel import TabPanel

from PyQt5.QtCore import Qt, QEvent, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QHBoxLayout, QLineEdit, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QVBoxLayout, QWidget, QLabel,
)

from tool import prefs as _prefs
from tool.shortcuts import shortcut_label_for

_CLI_PATH = Path(__file__).parent.parent / "cli.py"

# ---------------------------------------------------------------------------
# Constants mirrored from cli.py to avoid importing the full CLI module
# ---------------------------------------------------------------------------

_MODEL_KEYS = {
    "annotate":     "model_annotate",
    "segmentation": "model_segmentation",
    "embed":        "model_embed",
}
_MODEL_DEFAULTS = {
    "annotate":     "gemma4-e4b",
    "segmentation": "sam3.pt",
    "embed":        "BAAI/bge-small-en-v1.5",
}
_WEIGHT_FILE_EXTS = {".pt", ".pth", ".gguf", ".bin", ".onnx", ".engine"}

# (label, prefs_key, type, factory_default, lo, hi, step, decimals)
_DEFAULTS_SPEC: list[tuple] = [
    ("Frames per shot",     "annotate_frames_per_shot",     "int",   3,    1,   32,   1,   0),
    ("Min frame interval",  "annotate_min_frame_interval",  "float", 4.0,  0.5, 60.0, 0.5, 1),
    ("Max frames per shot", "annotate_max_frames_per_shot", "int",   16,   1,   64,   1,   0),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _local_models(project_path: str) -> list[str]:
    """Return names of model dirs and weight files under <project>/models/."""
    mdir = Path(project_path) / "models"
    if not mdir.exists():
        return []
    return [
        p.name
        for p in sorted(mdir.iterdir(), key=lambda x: x.name.lower())
        if p.is_dir() or (p.is_file() and p.suffix.lower() in _WEIGHT_FILE_EXTS)
    ]


def _style_canonical_form_label(label: QWidget) -> None:
    label.setStyleSheet(table_key_cell_style("", ""))
    label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    label.setFixedHeight(INSPECTOR_ROW_HEIGHT)


def _normalize_form_labels(form: QFormLayout) -> None:
    labels: list[QLabel] = []
    for row in range(form.rowCount()):
        item = form.itemAt(row, QFormLayout.LabelRole)
        if item is None:
            continue
        widget = item.widget()
        if isinstance(widget, QLabel):
            _style_canonical_form_label(widget)
            labels.append(widget)

    if not labels:
        return

    max_width = max(lbl.sizeHint().width() for lbl in labels)
    for lbl in labels:
        lbl.setFixedWidth(max_width)


# ---------------------------------------------------------------------------
# Project Visualizer V0 — fixed column dashboard (see services/corpus_stats.py
# for the ProjectColumn data model; this section is only a renderer over it)
# ---------------------------------------------------------------------------

_COLUMN_HEADER_H = 28   # fixed HEADER region height (px)
_COLUMN_COUNT_H = 64    # fixed COUNT region height (px)

# Display text for a column's status line when it isn't "ready" — keyed by
# ProjectColumn.reason first, falling back to a generic per-state label.
# Never invented data: purely maps the explicit missing/stale reason
# services.corpus_stats already reports into short display text.
_COLUMN_REASON_LABELS = {
    "illustration_index_missing": "INDEX REQUIRED",
    "illustration_index_stale": "INDEX STALE",
    "illustration_index_error": "INDEX ERROR",
    "corpus_stats_missing": "STATS REQUIRED",
    "corpus_stats_stale": "STATS STALE",
    "no_project": "NO PROJECT",
}
_COLUMN_STATE_FALLBACK_LABELS = {
    "unavailable": "UNAVAILABLE",
    "stale": "STALE",
}


def _format_column_count(count) -> str:
    """Format a ProjectColumn's raw int count for display (or an em dash).

    Purely a display-formatting helper — the underlying count itself always
    comes from services.corpus_stats, never invented here.
    """
    if count is None:
        return "\u2014"
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        text = f"{count / 1000:.1f}k"
    else:
        text = f"{count / 1_000_000:.1f}M"
    return text.replace(".0k", "k").replace(".0M", "M")


def _column_status_label(column) -> str:
    """Short status word for a non-ready column (e.g. "INDEX REQUIRED").

    Distinguishes *why* a column has no count — never rendered for a column
    that is genuinely, readily, zero.
    """
    return _COLUMN_REASON_LABELS.get(column.reason) or _COLUMN_STATE_FALLBACK_LABELS.get(
        column.state, "UNAVAILABLE",
    )


class _ProjectColumnWidget(QWidget):
    """One HEADER / COUNT / DATAVIS column of the Project Visualizer's V0 grid.

    A pure renderer over a single ``services.corpus_stats.ProjectColumn`` —
    it never computes or invents project data itself, only formats and lays
    out whatever column it is given via the constructor / ``set_column()``.
    ``column.state`` ("loading" / "ready" / "unavailable" / "stale") drives
    whether the shared SweepBar loading indicator (same one used by
    Illustration) is active and what the COUNT region shows. "unavailable"
    and "stale" are rendered distinctly from a real zero — both show a
    dimmed status word (e.g. "INDEX REQUIRED"/"INDEX STALE") instead of a
    number, so a missing/outdated artifact is never mistaken for "0".
    """

    def __init__(self, column, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: {theme.CELL_BG};")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._header_label = QLabel()
        self._header_label.setAlignment(Qt.AlignCenter)
        self._header_label.setFont(theme.font_ui(bold=True))
        self._header_label.setStyleSheet(
            f"background: {theme.TITLE_BG}; color: {theme.TEXT_DIM}; "
            f"border: 1px solid {theme.UI_BORDER};"
        )
        self._header_label.setFixedHeight(_COLUMN_HEADER_H)
        outer.addWidget(self._header_label)

        # Shared loading indicator (see visualizers/components/sweep_bar.py),
        # the same one Illustration uses — a thin ACCENT stripe, invisible
        # while idle. Driven by ProjectVisualizer's shared timer via tick().
        self._loading_bar = SweepBar(self)
        outer.addWidget(self._loading_bar)

        self._count_label = QLabel()
        self._count_label.setAlignment(Qt.AlignCenter)
        self._count_label.setWordWrap(True)
        self._ready_font = theme.font_mono(bold=True)
        self._ready_font.setPointSize(theme.BASE_PT + 10)
        self._status_font = theme.font_ui(bold=True)
        self._status_font.setPointSize(theme.BASE_PT + 1)
        self._count_label.setFont(self._ready_font)
        self._count_border_style = (
            f"border-left: 1px solid {theme.UI_BORDER}; "
            f"border-right: 1px solid {theme.UI_BORDER}; "
            f"border-bottom: 1px solid {theme.UI_BORDER};"
        )
        self._count_label.setStyleSheet(f"color: {theme.TEXT}; {self._count_border_style}")
        self._count_label.setFixedHeight(_COLUMN_COUNT_H)
        outer.addWidget(self._count_label)

        # DATAVIS region: every V0 column reports datavis={"kind": "empty"},
        # so this is currently just a distinct display-style area (matching
        # the CANVAS_BG convention used elsewhere for display surfaces) — not
        # yet a real visualization. A future version would dispatch on
        # column.datavis["kind"] here instead of always rendering blank.
        self._datavis_widget = QWidget()
        self._datavis_widget.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self._datavis_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer.addWidget(self._datavis_widget, 1)

        self.column = column
        self.set_column(column)

    def set_column(self, column) -> None:
        """Update this widget's displayed data for a refreshed ProjectColumn."""
        self.column = column
        self._header_label.setText(column.title.upper())
        if column.state == "loading":
            self._loading_bar.start()
            self._count_label.setFont(self._ready_font)
            self._count_label.setStyleSheet(f"color: {theme.TEXT}; {self._count_border_style}")
            self._count_label.setText("loading…")
        elif column.state == "ready":
            self._loading_bar.stop()
            self._count_label.setFont(self._ready_font)
            self._count_label.setStyleSheet(f"color: {theme.TEXT}; {self._count_border_style}")
            self._count_label.setText(_format_column_count(column.count))
        else:
            # "unavailable" / "stale" — never rendered the same as "0".
            self._loading_bar.stop()
            self._count_label.setFont(self._status_font)
            self._count_label.setStyleSheet(f"color: {theme.TEXT_DIM}; {self._count_border_style}")
            self._count_label.setText(f"\u2014\n{_column_status_label(column)}")


class _ProjectColumnsWorker(QThread):
    """Load Project Visualizer columns off the GUI thread.

    Follows the same QThread + signal pattern as Mosaic's VocabularyWorker /
    Illustration's index workers: heavy work happens in ``run()``, results
    are delivered to the GUI thread via a signal. Never performs a full
    corpus traversal and never silently rebuilds a missing/stale artifact —
    see services.corpus_stats.get_live_project_columns /
    get_cached_project_columns.

    Emits ``tier_ready(list)`` twice: once for the cheap "live" tier
    (Movies/Gameplay/Shots/Illustrations — computed directly from project
    files and the illustration index on every call), and again for the
    persisted-cache tier (Vocabulary/Segments/Flipbooks — reported as
    "unavailable"/"stale" rather than recomputed if the cache is missing or
    out of date) — so the GUI can display each tier as soon as it's ready
    instead of waiting for both.
    """

    tier_ready = pyqtSignal(list)  # list[ProjectColumn]

    def __init__(self, project_path, generation: int = 0, parent=None) -> None:
        super().__init__(parent)
        self.project_path = project_path
        self.generation = generation

    def run(self) -> None:
        from services.corpus_stats import get_cached_project_columns, get_live_project_columns
        self.tier_ready.emit(get_live_project_columns(self.project_path))
        self.tier_ready.emit(get_cached_project_columns(self.project_path))


# ---------------------------------------------------------------------------
# Launcher button
# ---------------------------------------------------------------------------


class _LauncherButton(QPushButton):
    """Visualizer launcher button whose name and F-key shortcut hint are
    laid out with a `QHBoxLayout` stretch (Qt's "horizontal spacer") so the
    shortcut always sits flush against the button's right edge, regardless
    of how long the visualizer name is. A single text string with fixed
    padding spaces (the previous approach) doesn't line up across buttons
    whose names differ in length.

    Painting the name/shortcut as child QLabels means they don't
    automatically pick up `theme.action_button_stylesheet()`'s hover/
    pressed/disabled QSS colors the way plain button text would, so this
    class mirrors those same state colors manually. Font weight is also
    set explicitly per label for the same reason: the name is bold, the
    shortcut hint stays normal weight.
    """

    def __init__(self, label: str, shortcut: str | None, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(6)

        self._name_label = QLabel(label)
        self._name_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._name_label.setFont(theme.font_ui(bold=True))
        layout.addWidget(self._name_label)

        layout.addStretch(1)

        self._shortcut_label = None
        if shortcut:
            self._shortcut_label = QLabel(shortcut)
            self._shortcut_label.setAttribute(Qt.WA_TransparentForMouseEvents)
            self._shortcut_label.setFont(theme.font_ui(bold=False))
            layout.addWidget(self._shortcut_label)

        self._apply_colors()

    def _apply_colors(self) -> None:
        if not self.isEnabled():
            color = dim_color = "rgba(255,255,255,0.15)"
        elif self.isDown():
            color = dim_color = theme.ACCENT
        elif self.underMouse():
            color = dim_color = theme.ACCENT_TEXT
        else:
            color = theme.TEXT
            dim_color = theme.TEXT_DIM
        self._name_label.setStyleSheet(f"color: {color}; background: transparent;")
        if self._shortcut_label is not None:
            self._shortcut_label.setStyleSheet(f"color: {dim_color}; background: transparent;")

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        super().setEnabled(enabled)
        self._apply_colors()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._apply_colors()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._apply_colors()

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self._apply_colors()

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self._apply_colors()

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        self._apply_colors()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class ProjectVisualizer(WindowVisualizer):

    def __init__(self) -> None:
        self._project_load_generation = 0
        self._project_load_state = "not_started"
        # Provide pref key to the shell so geometry is saved/restored there
        super().__init__(pref_key="window_project")
        self.setWindowTitle("Crossing — Project")
        self._procs: dict[str, subprocess.Popen] = {}
        self._windows: dict[str, object] = {}  # in-process visualizer windows
        self._backup_proc: subprocess.Popen | None = None
        self._backup_poll_timer: QTimer | None = None
        self._backup_master_fd: int = -1
        self._backup_stdout_buf: bytes = b""
        self._backup_anim_frame: int = 0

        # visual sizing hint
        self.setMinimumSize(900, 560)

        # Other visualizers construct a ProjectVisualizer purely as an
        # internal, never-shown "hub" (to call `_launch()` on) when this
        # process doesn't already have one. That hub must NOT count as a
        # real, already-open "Project" window — otherwise a later F1 press
        # would incorrectly reveal it instead of raising/pinging whichever
        # process (this one or another) actually has Project legitimately
        # open. This flag flips True the first time the window is actually
        # shown (see `showEvent`), regardless of which path did the showing.
        self._shown_as_project = False

    # geometry handled by WindowVisualizer (pref_key passed at construction)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._shown_as_project = True

    def closeEvent(self, event) -> None:
        timer = getattr(self, "_column_loading_timer", None)
        if timer is not None:
            timer.stop()
        worker = getattr(self, "_project_columns_worker", None)
        if worker is not None and worker.isRunning():
            worker.wait(3000)
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Project path

    def _build_folder_section(self) -> CollapsibleSection:
        sec = CollapsibleSection("Folder", pref_key="project_section_folder")
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(theme.SECTION_GAP)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("(no project folder set)")
        self.path_edit.setText(_prefs.get("path") or "")
        self.path_edit.setFont(theme.font_ui(bold=True))
        row.addWidget(self.path_edit, 1)

        outer.addLayout(row)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse)
        browse_btn.setStyleSheet(theme.action_button_stylesheet())
        outer.addWidget(browse_btn)

        row_widget = QWidget()
        row_widget.setLayout(outer)
        sec.add_widget(row_widget)
        return sec

    def _on_browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Project Folder",
            self.path_edit.text() or str(Path.home()),
        )
        if folder:
            _prefs.set("path", folder)
            self.path_edit.setText(folder)
            self._reload_model_combos()
            self._start_project_columns_load(force=True)

    # ------------------------------------------------------------------
    # Backup path

    def _build_backup_section(self) -> CollapsibleSection:
        sec = CollapsibleSection("Backup", pref_key="project_section_backup")
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(theme.SECTION_GAP)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.backup_path_edit = QLineEdit()
        self.backup_path_edit.setReadOnly(True)
        self.backup_path_edit.setPlaceholderText("(no backup folder set)")
        self.backup_path_edit.setText(_prefs.get("backup_path") or "")
        self.backup_path_edit.setFont(theme.font_ui(bold=True))
        row.addWidget(self.backup_path_edit, 1)

        outer.addLayout(row)

        self.backup_browse_btn = QPushButton("Browse…")
        self.backup_browse_btn.clicked.connect(self._on_backup_browse)
        self.backup_browse_btn.setStyleSheet(theme.action_button_stylesheet())
        self.backup_browse_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.backup_btn = QPushButton("Backup")
        self.backup_btn.clicked.connect(self._on_backup_run)
        self.backup_btn.setStyleSheet(theme.action_button_stylesheet())
        self.backup_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(0, 0, 0, 0)
        buttons_row.setSpacing(theme.SECTION_GAP)
        buttons_row.addWidget(self.backup_browse_btn, 1)
        buttons_row.addWidget(self.backup_btn, 1)
        outer.addLayout(buttons_row)

        outer_widget = QWidget()
        outer_widget.setLayout(outer)
        sec.add_widget(outer_widget)

        # Accent sweep-bar shown on the section title while a backup is
        # running in the background — same canonical loading indicator used
        # by Cloud's Tools section and Illustration's Silhouettes/Engravings.
        self._backup_loading_bar = SweepBar(self)
        self._backup_loading_timer = QTimer(self)
        self._backup_loading_timer.setInterval(20)   # ~50 fps
        self._backup_loading_timer.timeout.connect(self._backup_loading_bar.tick)
        sec.set_subbar(self._backup_loading_bar)

        self._refresh_backup_button()
        return sec

    def _on_backup_browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Backup Folder",
            self.backup_path_edit.text() or str(Path.home()),
        )
        if folder:
            _prefs.set("backup_path", folder)
            self.backup_path_edit.setText(folder)
            self._refresh_backup_button()

    def _refresh_backup_button(self) -> None:
        """Enable/disable the Backup button based on lightweight path checks."""
        import shutil
        backup_path = _prefs.get("backup_path")
        if not backup_path:
            self.backup_btn.setEnabled(False)
            return
        p = Path(backup_path)
        if not p.exists() or not os.access(str(p), os.W_OK):
            self.backup_btn.setEnabled(False)
            return
        # Optionally verify disk_usage is accessible (non-fatal if not)
        try:
            shutil.disk_usage(str(p))
        except Exception:
            pass
        self.backup_btn.setEnabled(True)

    def _on_backup_run(self) -> None:
        import fcntl
        import pty
        if self._backup_proc is not None and self._backup_proc.poll() is None:
            self._stop_backup_proc()
            return  # Clicking while running stops the backup instead.

        project_path = _prefs.get("path")
        if not project_path:
            QMessageBox.warning(self, "No Project", "Please set a project folder first.")
            return

        cmd = [sys.executable, str(_CLI_PATH), "backup", "update"]
        try:
            # Use a pty so rsync believes it is writing to a terminal and
            # flushes progress updates immediately instead of buffering.
            master_fd, slave_fd = pty.openpty()
            self._backup_proc = subprocess.Popen(
                cmd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
            self._backup_master_fd = master_fd
            # Non-blocking reads so the timer never stalls
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except Exception as exc:
            QMessageBox.critical(self, "Backup failed", str(exc))
            return

        self._backup_stdout_buf = b""
        self._backup_anim_frame = 0
        self.backup_browse_btn.setEnabled(False)
        self.backup_btn.setText("Backing Up")
        self._backup_loading_bar.start()
        self._backup_loading_timer.start()

        self._backup_poll_timer = QTimer(self)
        self._backup_poll_timer.setInterval(500)
        self._backup_poll_timer.timeout.connect(self._poll_backup_proc)
        self._backup_poll_timer.start()

    def _stop_backup_proc(self) -> None:
        """Terminate a running backup process (user clicked Backup while it was running).

        UI cleanup (re-enabling Browse, resetting the button text, stopping
        the section's loading animation) happens on the next
        ``_poll_backup_proc`` tick once ``poll()`` reports the process exited.
        """
        if self._backup_proc is not None:
            try:
                self._backup_proc.terminate()
            except Exception:
                pass

    def _poll_backup_proc(self) -> None:
        """Called every 500 ms to drain pty output and detect completion."""
        # Drain any available output from the pty master
        try:
            chunk = os.read(self._backup_master_fd, 4096)
            self._backup_stdout_buf += chunk
        except (BlockingIOError, OSError):
            pass

        # Animate the button label: "Backing Up", "Backing Up.", "Backing Up..", "Backing Up..."
        self._backup_anim_frame = (self._backup_anim_frame + 1) % 4
        dots = "." * self._backup_anim_frame
        self.backup_btn.setText(f"Backing Up{dots}")

        # Check if the process has finished
        if self._backup_proc is None or self._backup_proc.poll() is not None:
            self._backup_poll_timer.stop()
            rc = self._backup_proc.returncode if self._backup_proc else -1
            try:
                os.close(self._backup_master_fd)
            except OSError:
                pass
            self._backup_proc = None
            self._backup_loading_timer.stop()
            self._backup_loading_bar.stop()
            self.backup_browse_btn.setEnabled(True)
            self.backup_btn.setText("Backup")

    # ------------------------------------------------------------------
    # Defaults

    def _build_defaults_section(self) -> CollapsibleSection:
        sec = CollapsibleSection("Defaults", pref_key="project_section_defaults")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(theme.SECTION_GAP)
        form.setHorizontalSpacing(0)
        form.setVerticalSpacing(theme.SECTION_GAP)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)

        self._default_widgets: dict = {}
        for label, key, typ, default, lo, hi, step, dec in _DEFAULTS_SPEC:
            current = _prefs.get(key, default)
            if typ == "int":
                w = QSpinBox()
                w.setRange(lo, hi)
                w.setSingleStep(step)
                w.setValue(int(current))
                w.valueChanged.connect(lambda v, k=key: _prefs.set(k, v))
            else:
                w = QDoubleSpinBox()
                w.setRange(lo, hi)
                w.setSingleStep(step)
                w.setDecimals(dec)
                w.setValue(float(current))
                w.valueChanged.connect(lambda v, k=key: _prefs.set(k, v))
            w.setFont(theme.font_ui(bold=True))
            self._default_widgets[key] = w
            form.addRow(label, w)

        _normalize_form_labels(form)

        form_widget = QWidget()
        form_widget.setLayout(form)
        sec.add_widget(form_widget)
        return sec

    # ------------------------------------------------------------------
    # Models

    def _build_models_section(self) -> CollapsibleSection:
        sec = CollapsibleSection("Models", pref_key="project_section_models")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(theme.SECTION_GAP)
        form.setHorizontalSpacing(0)
        form.setVerticalSpacing(theme.SECTION_GAP)

        self._model_combos: dict[str, QComboBox] = {}
        for role, key in _MODEL_KEYS.items():
            combo = QComboBox()
            combo.currentTextChanged.connect(
                lambda text, k=key: _prefs.set(k, text) if text else None
            )
            style_canonical_combo(combo)
            self._model_combos[role] = combo
            form.addRow(role.capitalize(), combo)

        _normalize_form_labels(form)

        form_widget = QWidget()
        form_widget.setLayout(form)
        sec.add_widget(form_widget)
        self._reload_model_combos()
        return sec

    def _reload_model_combos(self) -> None:
        path = _prefs.get("path") or ""
        local = _local_models(path) if path else []
        for role, combo in self._model_combos.items():
            key = _MODEL_KEYS[role]
            current = _prefs.get(key) or _MODEL_DEFAULTS[role]
            combo.blockSignals(True)
            combo.clear()
            # Current value always first; local models de-duplicated after it
            options = list(dict.fromkeys([current] + local))
            for opt in options:
                combo.addItem(opt)
            combo.setCurrentText(current)
            combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Media import

    def _build_import_section(self) -> CollapsibleSection:
        sec = CollapsibleSection("Import", pref_key="project_section_import")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(theme.SECTION_GAP)
        form.setHorizontalSpacing(0)
        form.setVerticalSpacing(theme.SECTION_GAP)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.media_type_combo = QComboBox()
        self.media_type_combo.addItems(["movie", "gameplay"])
        style_canonical_combo(self.media_type_combo)
        form.addRow("Type", self.media_type_combo)

        self.media_game_edit = QLineEdit()
        self.media_game_edit.setPlaceholderText("game slug (e.g. rdr2)")
        form.addRow("Game", self.media_game_edit)
        # Keep a reference to the game row widgets so we can show/hide them
        self._media_game_label = form.itemAt(form.rowCount() - 1, QFormLayout.LabelRole).widget()
        self._media_game_field = form.itemAt(form.rowCount() - 1, QFormLayout.FieldRole).widget()

        _normalize_form_labels(form)

        import_btn = QPushButton("Import")
        import_btn.clicked.connect(self._on_media_import)
        import_btn.setStyleSheet(theme.action_button_stylesheet())

        self.media_type_combo.currentTextChanged.connect(self._on_media_type_changed)
        # Set initial visibility
        self._on_media_type_changed(self.media_type_combo.currentText())

        form_widget = QWidget()
        form_widget.setLayout(form)
        sec.add_widget(form_widget)
        sec.add_widget(import_btn)
        return sec

    def _on_media_type_changed(self, media_type: str):
        visible = (media_type == "gameplay")
        self._media_game_label.setVisible(visible)
        self._media_game_field.setVisible(visible)

    def _on_media_import(self):
        project_path = _prefs.get("path")
        if not project_path:
            QMessageBox.warning(self, "No Project", "Please set a project folder first.")
            return

        files, _ = QFileDialog.getOpenFileNames(
            self, "Select file(s) to import", "",
            "Video files (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.mpg *.mpeg);;All files (*)",
        )
        if not files:
            return

        media = self.media_type_combo.currentText()
        cmd = [
            sys.executable, str(_CLI_PATH),
            "media", "import",
            "--media", media,
        ]
        if media == "gameplay":
            game = self.media_game_edit.text().strip()
            if not game:
                QMessageBox.warning(self, "Game required", "Please enter a game slug for gameplay imports.")
                return
            cmd += ["--game", game]
        cmd += files

        try:
            subprocess.Popen(cmd)
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))

    # Launcher buttons

    def _build_visualizers_section(self) -> CollapsibleSection:
        sec = CollapsibleSection("Visualizers", pref_key="project_section_visualizers")

        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(theme.SECTION_GAP)

        for (label, sub, enabled), (row, col) in zip(
            [
                ("Metadata",     "metadata",     True),
                ("Shotlist",     "shotlist",     True),
                ("Mosaic",       "mosaic",       True),
                ("Cloud",        "cloud",        True),
                ("Segmentation", "segmentation", True),
                ("Illustration", "illustration", True),
                ("Flipbook",     "flipbook",     True),
                ("Palette",      "palette",      True),
                ("Book",         "book",         True),
                ("Sync",         "sync",         True),
            ],
            [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1), (4, 0), (4, 1)],
        ):
            # Shortcut label (e.g. "F9") comes solely from the shared
            # tool.shortcuts.FUNCTION_KEY_BINDINGS mapping, never hardcoded
            # here, so the button text can't drift out of sync with the
            # actual F-key binding.
            shortcut = shortcut_label_for(sub)
            btn = _LauncherButton(label, shortcut)
            btn.setEnabled(enabled)
            btn.setStyleSheet(theme.action_button_stylesheet())
            if enabled:
                btn.clicked.connect(lambda _, s=sub: self._launch(s))
            grid.addWidget(btn, row, col)

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(grid_widget)

        outer_widget = QWidget()
        outer_widget.setLayout(outer)
        sec.add_widget(outer_widget)
        return sec

    def create_inspector(self) -> QWidget:
        inspector = Inspector(self)

        panel = TabPanel()
        panel.add_widget(self._build_visualizers_section())
        panel.add_widget(self._build_folder_section())
        panel.add_widget(self._build_backup_section())
        panel.add_widget(self._build_models_section())
        panel.add_widget(self._build_defaults_section())
        panel.add_widget(self._build_import_section())

        inspector.add_tab(panel, " Project ")

        self._inspector = inspector
        return inspector


    def create_browser(self) -> QWidget:
        from services.corpus_stats import ProjectColumn, PROJECT_COLUMN_IDS_AND_TITLES

        w = QWidget()
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        w.setStyleSheet(f"background: {theme.CANVAS_BG};")

        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        # Headers are created immediately, in a "loading" state — no data
        # access happens on the GUI thread. _start_project_columns_load()
        # kicks off the background worker that fills them in.
        self._project_column_widgets: dict[str, _ProjectColumnWidget] = {}
        for col_id, title in PROJECT_COLUMN_IDS_AND_TITLES:
            placeholder = ProjectColumn(
                id=col_id, title=title, count=None, datavis={"kind": "empty"},
                state="loading",
            )
            col_widget = _ProjectColumnWidget(placeholder)
            self._project_column_widgets[col_id] = col_widget
            layout.addWidget(col_widget, 1)

        self._project_columns_worker: Optional[_ProjectColumnsWorker] = None
        self._column_loading_timer = QTimer(self)
        self._column_loading_timer.setInterval(20)
        self._column_loading_timer.timeout.connect(self._tick_column_loading_bars)

        self._start_project_columns_load()
        return w

    def _tick_column_loading_bars(self) -> None:
        for widget in self._project_column_widgets.values():
            widget._loading_bar.tick()

    def _start_project_columns_load(self, *, force: bool = False) -> None:
        """(Re)start the background worker that fills in every column.

        Marks every column "loading" immediately (synchronous, GUI-thread —
        just updating already-built label widgets, no project data access),
        then hands the actual work to a QThread so opening/reloading the
        Project Visualizer never blocks on a corpus traversal.
        """
        if not getattr(self, "_project_column_widgets", None):
            return
        if not force and self._project_load_state != "not_started":
            return

        from services.corpus_stats import ProjectColumn

        if self._project_columns_worker is not None and self._project_columns_worker.isRunning():
            self._project_columns_worker.tier_ready.disconnect(self._on_columns_tier_ready)
            self._project_columns_worker.wait(3000)

        for col_id, widget in self._project_column_widgets.items():
            loading = ProjectColumn(
                id=col_id, title=widget.column.title, count=None,
                datavis={"kind": "empty"}, state="loading",
            )
            widget.set_column(loading)
        self._column_loading_timer.start()
        self._project_load_state = "loading"

        self._project_load_generation += 1
        generation = self._project_load_generation
        worker = _ProjectColumnsWorker(_prefs.get("path"), generation)
        worker.tier_ready.connect(self._on_columns_tier_ready)
        self._project_columns_worker = worker
        worker.start()

    def _on_columns_tier_ready(
        self,
        columns,
    ) -> None:
        for column in columns:
            widget = self._project_column_widgets.get(column.id)
            if widget is not None:
                widget.set_column(column)
        if all(w.column.state != "loading" for w in self._project_column_widgets.values()):
            self._column_loading_timer.stop()
            self._project_load_state = "loaded"

    # Splitter/panel behavior provided by WindowVisualizer

    def _launch(self, subcommand: str) -> None:
        if not _prefs.get("path"):
            QMessageBox.warning(self, "No Project", "Please set a project folder first.")
            return

        # Raise an already-open in-process window (works because all visualizers
        # opened via this launcher share the same QApplication event loop).
        from visualizers._window_helpers import raise_existing_window
        if raise_existing_window(subcommand):
            return

        project_path = _prefs.get("path")

        # Cross-process single-instance guard: if *subcommand* is already
        # open in a *different* OS process (e.g. launched directly via
        # `crossing visualizer <name>`), ping that process to raise its
        # window instead of creating a duplicate here. Illustration and
        # Shotlist are excluded — they already have their own bespoke
        # cross-process IPC (navigation payloads, not just raising).
        from visualizers.components.singleton_guard import (
            claim_or_ping_and_bind, SELF_MANAGED_SUBCOMMANDS,
        )
        if subcommand not in SELF_MANAGED_SUBCOMMANDS:
            if not claim_or_ping_and_bind(subcommand, project_path, QApplication.instance()):
                return  # another process owns it; it was pinged to raise itself

        # Open the visualizer in-process so future raises are always reliable.
        try:
            win = self._create_in_process_window(subcommand, project_path)
        except Exception as exc:
            import traceback
            QMessageBox.critical(
                self, "Error",
                f"Could not open {subcommand} visualizer:\n{exc}\n\n{traceback.format_exc()}",
            )
            return

        if win is not None:
            win.show()
            self._windows[subcommand] = win  # keep reference so Qt doesn't GC it
            return

        # For the Illustration visualizer we must not construct its QWidget
        # inside this process. _create_in_process_window() will have delegated
        # to the canonical IPC/launcher path; nothing more to do here.
        if subcommand == "illustration":
            return

        # Fallback for subcommands not handled in-process (e.g. shotlist).
        # For shotlist specifically, ping its IPC socket to raise the window.
        if subcommand == "shotlist":
            project_path = _prefs.get("path") or ""
            try:
                from visualizers.shot_visualizer import ipc_send_load, _ipc_socket_path
                if _ipc_socket_path(project_path).exists():
                    ipc_send_load(project_path, "", "movie")
                    return
            except Exception:
                pass

        proc = self._procs.get(subcommand)
        if proc is not None and proc.poll() is None:
            return  # already running, nothing more we can do without OS tools

        self._procs[subcommand] = subprocess.Popen(
            [sys.executable, str(_CLI_PATH), "visualizer", subcommand]
        )

    def _create_in_process_window(self, subcommand: str, project_path: str):
        """Instantiate the named visualizer as a window inside this process.

        Returns the window (not yet shown) or None for subcommands that must
        run as separate processes (e.g. shotlist with its own IPC server).
        """
        media_type = "movie"
        if subcommand == "metadata":
            from visualizers.metadata_visualizer import MetadataVisualizer
            return MetadataVisualizer(project_path)
        elif subcommand == "cloud":
            from visualizers.cloud_visualizer import CloudVisualizer
            return CloudVisualizer(project_path)
        elif subcommand == "mosaic":
            from visualizers.mosaic_visualizer import MosaicVisualizer
            return MosaicVisualizer(project_path, media_type=media_type)
        elif subcommand == "book":
            from visualizers.book_visualizer import BookVisualizerWindow
            return BookVisualizerWindow(project_path)
        elif subcommand == "illustration":
            # Per the new architecture, never instantiate another visualizer's
            # QWidget hierarchy inside this process. Instead request navigation
            # via the Illustration helper which will try IPC and otherwise
            # delegate process creation to the canonical launcher.
            from visualizers.illustration_visualizer import open_at_illustration
            open_at_illustration(project_path, media_type=media_type)
            return None
        elif subcommand == "palette":
            from visualizers.palette_visualizer import PaletteVisualizerWindow
            return PaletteVisualizerWindow(project_path, media_type=media_type)
        elif subcommand == "flipbook":
            import visualizers.flipbook_visualizer as _fv
            _fv._FONT_FAMILY = _fv._load_flipbook_font()
            return _fv.FlipbookVisualizerWindow(project_path, media_type=media_type)
        elif subcommand == "sync":
            from visualizers.sync_visualizer import SyncVisualizerWindow
            return SyncVisualizerWindow()
        elif subcommand == "segmentation":
            model_name = _prefs.get("model_segmentation", "sam3.pt") or "sam3.pt"
            from visualizers.segmentation_visualizer import SAMExplorer
            return SAMExplorer(project_path, model_name=model_name)
        return None  # caller falls through to subprocess

    # Keyboard handled by WindowVisualizer (Tab/Shift+Tab/Esc/Ctrl+Q/Ctrl+W)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer() -> None:
    """Create QApplication (if needed) and launch the project window."""
    from visualizers.launcher import run_visualizer_window
    run_visualizer_window("project", lambda: ProjectVisualizer(), check_existing=False)


if __name__ == "__main__":
    run_visualizer()
