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

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import JumpScrollBar
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.metadata_block import INSPECTOR_ROW_HEIGHT, table_key_cell_style

from PyQt5.QtCore import Qt, QEvent, QTimer
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLineEdit, QListView, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout, QWidget, QLabel,
)

from tool import prefs as _prefs

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


def _style_canonical_combo(combo: QComboBox) -> None:
    """Apply the canonical visualizer combo font and popup styling."""
    combo.setFocusPolicy(Qt.NoFocus)
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLength)
    combo.setFont(theme.font_ui())
    combo.setStyleSheet(
        f"QComboBox {{"
        f"  background: {theme.BTN_BG}; color: {theme.TEXT};"
        f"  border: none; border-radius: 3px; padding: 0px 6px;"
        f"  min-height: 24px; max-height: 24px;"
        f"  font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
        f"  font-weight: {theme.WEIGHT_UI};"
        f"}}"
        f"QComboBox::drop-down {{ border: none; }}"
        f"QComboBox QAbstractItemView, QComboBox QListView {{"
        f"  background: {theme.INPUT_BG}; color: {theme.TEXT};"
        f"  border: 0px; margin: 0px; padding: 0px; outline: 0px;"
        f"  selection-background-color: {theme.ACCENT};"
        f"  selection-color: {theme.ACCENT_TEXT};"
        f"  font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
        f"}}"
        f"QComboBox QAbstractItemView::item, QComboBox QListView::item {{"
        f"  padding: 0px 8px; min-height: 24px; border: 0px;"
        f"}}"
    )
    view = QListView(combo)
    view.setUniformItemSizes(True)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    view.setFrameShape(QFrame.NoFrame)
    view.setLineWidth(0)
    view.setMidLineWidth(0)
    view.setContentsMargins(0, 0, 0, 0)
    view.setFont(theme.font_ui())
    view.setStyleSheet(
        f"QListView {{ background: {theme.INPUT_BG}; color: {theme.TEXT};"
        f" border: 0px; margin: 0px; padding: 0px; outline: 0px;"
        f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt; }}"
        f"QListView::item {{ background: {theme.INPUT_BG}; padding: 0px 8px;"
        f" min-height: 24px; border: 0px; }}"
        f"QListView::item:selected {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
    )
    combo.setView(view)


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
# Main window
# ---------------------------------------------------------------------------

_VISUALIZER_TITLE = {
    "shotlist":    "Shotlist Visualizer",
    "mosaic":      "Mosaic Visualizer",
    "metadata":    "Metadata Visualizer",
    "cloud":       "Cloud Visualizer",
    "illustration": "Illustration Visualizer",
    "palette":     "Palette Visualizer",
    "flipbook":    "Flipbook Visualizer",
    "sync":        "Sync Visualizer",
}


class ProjectVisualizer(WindowVisualizer):

    def __init__(self) -> None:
        # Provide pref key to the shell so geometry is saved/restored there
        super().__init__(pref_key="window_project")
        self.setWindowTitle("Crossing — Project Visualizer")
        self._procs: dict[str, subprocess.Popen] = {}
        self._windows: dict[str, object] = {}  # in-process visualizer windows
        self._backup_proc: subprocess.Popen | None = None
        self._backup_poll_timer: QTimer | None = None
        self._backup_master_fd: int = -1
        self._backup_stdout_buf: bytes = b""
        self._backup_anim_frame: int = 0

        # visual sizing hint
        self.setMinimumSize(900, 560)

    # geometry handled by WindowVisualizer (pref_key passed at construction)

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
        row.addWidget(self.backup_path_edit, 1)

        outer.addLayout(row)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_backup_browse)
        browse_btn.setStyleSheet(theme.action_button_stylesheet())
        browse_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.backup_btn = QPushButton("Backup")
        self.backup_btn.clicked.connect(self._on_backup_run)
        self.backup_btn.setStyleSheet(theme.action_button_stylesheet())
        self.backup_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(0, 0, 0, 0)
        buttons_row.setSpacing(theme.SECTION_GAP)
        buttons_row.addWidget(browse_btn, 1)
        buttons_row.addWidget(self.backup_btn, 1)
        outer.addLayout(buttons_row)

        outer_widget = QWidget()
        outer_widget.setLayout(outer)
        sec.add_widget(outer_widget)

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
            return  # Already running

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
        self.backup_btn.setEnabled(False)
        self.backup_btn.setStyleSheet(
            "QPushButton { background-color: #CC00CC; color: white; font-weight: bold; }"
        )
        self.backup_btn.setText("Backing Up")

        self._backup_poll_timer = QTimer(self)
        self._backup_poll_timer.setInterval(500)
        self._backup_poll_timer.timeout.connect(self._poll_backup_proc)
        self._backup_poll_timer.start()

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
            self.backup_btn.setEnabled(True)
            self.backup_btn.setText("Backup")
            self.backup_btn.setStyleSheet("")

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
            _style_canonical_combo(combo)
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
        _style_canonical_combo(self.media_type_combo)
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
        grid.setSpacing(2)

        for (label, sub, enabled), (row, col) in zip(
            [
                ("Metadata",     "metadata",     True),
                ("Shotlist",     "shotlist",     True),
                ("Mosaic",       "mosaic",       True),
                ("Cloud",        "cloud",        True),
                ("Book",         "book",         True),
                ("Illustration", "illustration", True),
                ("Palette",      "palette",      True),
                ("Flipbook",     "flipbook",     True),
                ("Sync",         "sync",         True),
                ("Segmentation", "segmentation", True),
            ],
            [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1), (4, 0), (4, 1)],
        ):
            btn = QPushButton(label)
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
        outer = QWidget()
        outer.setStyleSheet(f"background: {theme.CANVAS_BG};")

        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.tabBar().setDrawBase(False)
        tabs.tabBar().setExpanding(False)
        tabs.tabBar().setUsesScrollButtons(False)
        tabs.setFocusPolicy(Qt.NoFocus)
        tabs.tabBar().setFocusPolicy(Qt.NoFocus)
        tabs.setStyleSheet(theme.tab_strip_stylesheet())

        project_tab = QWidget()
        project_tab.setStyleSheet(f"background: {theme.TAB_BG};")
        project_tab_layout = QVBoxLayout(project_tab)
        project_tab_layout.setContentsMargins(0, 0, 0, 0)
        project_tab_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setFocusPolicy(Qt.NoFocus)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBar(JumpScrollBar())
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {theme.TAB_BG}; border: none; }}"
            f"QScrollBar:vertical {{ background: {theme.CANVAS_BG}; width: {theme.SCROLLBAR_W}px; }}"
            f"QScrollBar::groove:vertical {{ background: transparent; border: none; }}"
            f"QScrollBar::handle:vertical {{"
            f"  background: transparent; border-left: 2px solid {theme.ACCENT};"
            f"  border-radius: 0; min-height: 20px; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}"
        )
        project_tab_layout.addWidget(scroll)

        content = QWidget()
        content.setStyleSheet(f"background: {theme.TAB_BG};")
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(
            theme.SECTION_GAP, theme.SECTION_GAP, theme.SECTION_GAP, theme.SECTION_GAP
        )
        layout.setSpacing(theme.SECTION_GAP)
        layout.setAlignment(Qt.AlignTop)

        folder_sec = self._build_folder_section()
        backup_sec = self._build_backup_section()
        defaults_sec = self._build_defaults_section()
        models_sec = self._build_models_section()
        import_sec = self._build_import_section()
        visualizers_sec = self._build_visualizers_section()

        layout.addWidget(folder_sec)
        layout.addWidget(backup_sec)
        layout.addWidget(defaults_sec)
        layout.addWidget(models_sec)
        layout.addWidget(import_sec)
        layout.addWidget(visualizers_sec)

        tabs.addTab(project_tab, " Project ")
        outer_layout.addWidget(tabs)

        self._inspector_scroll = scroll
        self._inspector_tabs = tabs
        return outer

    def create_browser(self) -> QWidget:
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        w.setStyleSheet(f"background: {theme.CANVAS_BG};")
        return w

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

        # Open the visualizer in-process so future raises are always reliable.
        project_path = _prefs.get("path")
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
            from visualizers.illustration_visualizer import SAMExplorer
            return SAMExplorer(project_path, model_name=model_name)
        return None  # caller falls through to subprocess

    # Keyboard handled by WindowVisualizer (Tab/Shift+Tab/Esc/Ctrl+Q/Ctrl+W)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer() -> None:
    """Create QApplication (if needed) and launch the project window."""
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    win = ProjectVisualizer()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    run_visualizer()
