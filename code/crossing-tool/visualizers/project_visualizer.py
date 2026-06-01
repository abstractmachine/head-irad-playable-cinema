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
from styles.theme import save_window_geometry, restore_window_geometry

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLineEdit, QPushButton,
    QComboBox, QFormLayout, QDoubleSpinBox, QSpinBox,
    QFileDialog, QMessageBox,
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
    "segmentation": "sam2.1_b.pt",
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


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

_VISUALIZER_TITLE = {
    "shotlist":    "Shotlist Visualizer",
    "mosaic":      "Mosaic Visualizer",
    "composition": "Composition Visualizer",
    "metadata":    "Metadata Visualizer",
    "cloud":       "Cloud Visualizer",
    "silhouette":  "Silhouette Visualizer",
    "palette":     "Palette Visualizer",
    "flipbook":    "Flipbook Visualizer",
}


class ProjectVisualizer(QMainWindow):

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Crossing — Project")
        self._procs: dict[str, subprocess.Popen] = {}
        self._backup_proc: subprocess.Popen | None = None
        self._backup_poll_timer: QTimer | None = None
        self._backup_master_fd: int = -1
        self._backup_stdout_buf: bytes = b""
        self._backup_anim_frame: int = 0

        root = QWidget()
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addWidget(self._build_project_group())
        layout.addWidget(self._build_backup_group())
        layout.addWidget(self._build_defaults_group())
        layout.addWidget(self._build_models_group())
        layout.addWidget(self._build_media_group())
        layout.addWidget(self._build_launchers_group())

        self.setMinimumWidth(480)
        restore_window_geometry(self, "window_project")

    def closeEvent(self, event) -> None:
        save_window_geometry(self, "window_project")
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Project path

    def _build_project_group(self) -> QGroupBox:
        group = QGroupBox("Project")
        row = QHBoxLayout(group)
        row.setContentsMargins(8, 12, 8, 8)
        row.setSpacing(6)

        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("(no project folder set)")
        self.path_edit.setText(_prefs.get("path") or "")
        row.addWidget(self.path_edit, 1)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse)
        row.addWidget(browse_btn)

        return group

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

    def _build_backup_group(self) -> QGroupBox:
        group = QGroupBox("Backup")
        outer = QVBoxLayout(group)
        outer.setContentsMargins(8, 12, 8, 8)
        outer.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(6)

        self.backup_path_edit = QLineEdit()
        self.backup_path_edit.setReadOnly(True)
        self.backup_path_edit.setPlaceholderText("(no backup folder set)")
        self.backup_path_edit.setText(_prefs.get("backup_path") or "")
        row.addWidget(self.backup_path_edit, 1)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_backup_browse)
        row.addWidget(browse_btn)

        outer.addLayout(row)

        self.backup_btn = QPushButton("Backup")
        self.backup_btn.clicked.connect(self._on_backup_run)
        outer.addWidget(self.backup_btn)

        self._refresh_backup_button()
        return group

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

    _BACKUP_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def _poll_backup_proc(self) -> None:
        # Read any available bytes from the pty master fd (keep buffer trimmed)
        if self._backup_master_fd >= 0:
            try:
                chunk = os.read(self._backup_master_fd, 4096)
                if chunk:
                    self._backup_stdout_buf += chunk
                    if len(self._backup_stdout_buf) > 8192:
                        self._backup_stdout_buf = self._backup_stdout_buf[-4096:]
            except (BlockingIOError, OSError):
                pass

        # Advance spinner regardless of whether new bytes arrived
        frame = self._BACKUP_SPINNER[self._backup_anim_frame % len(self._BACKUP_SPINNER)]
        self._backup_anim_frame += 1
        self.backup_btn.setText(f"{frame}  Backing Up")

        if self._backup_proc is None or self._backup_proc.poll() is not None:
            if self._backup_master_fd >= 0:
                try:
                    os.close(self._backup_master_fd)
                except OSError:
                    pass
                self._backup_master_fd = -1
            if self._backup_poll_timer is not None:
                self._backup_poll_timer.stop()
                self._backup_poll_timer = None
            self._backup_proc = None
            self.backup_btn.setStyleSheet("")
            self.backup_btn.setText("Backup")
            self._refresh_backup_button()

    # ------------------------------------------------------------------
    # Defaults

    def _build_defaults_group(self) -> QGroupBox:
        group = QGroupBox("Defaults")
        form = QFormLayout(group)
        form.setContentsMargins(8, 12, 8, 8)
        form.setSpacing(5)
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

        return group

    # ------------------------------------------------------------------
    # Models

    def _build_models_group(self) -> QGroupBox:
        group = QGroupBox("Models")
        form = QFormLayout(group)
        form.setContentsMargins(8, 12, 8, 8)
        form.setSpacing(5)

        self._model_combos: dict[str, QComboBox] = {}
        for role, key in _MODEL_KEYS.items():
            combo = QComboBox()
            combo.currentTextChanged.connect(
                lambda text, k=key: _prefs.set(k, text) if text else None
            )
            self._model_combos[role] = combo
            form.addRow(role.capitalize(), combo)

        self._reload_model_combos()
        return group

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

    def _build_media_group(self) -> QGroupBox:
        group = QGroupBox("Import Media")
        form = QFormLayout(group)
        form.setContentsMargins(8, 12, 8, 8)
        form.setSpacing(6)

        self.media_type_combo = QComboBox()
        self.media_type_combo.addItems(["movie", "gameplay"])
        form.addRow("Type", self.media_type_combo)

        self.media_game_edit = QLineEdit()
        self.media_game_edit.setPlaceholderText("game slug (e.g. rdr2)")
        form.addRow("Game", self.media_game_edit)
        # Keep a reference to the game row widgets so we can show/hide them
        self._media_game_label = form.itemAt(form.rowCount() - 1, QFormLayout.LabelRole).widget()
        self._media_game_field = form.itemAt(form.rowCount() - 1, QFormLayout.FieldRole).widget()

        import_btn = QPushButton("Import")
        import_btn.clicked.connect(self._on_media_import)
        form.addRow("", import_btn)

        self.media_type_combo.currentTextChanged.connect(self._on_media_type_changed)
        # Set initial visibility
        self._on_media_type_changed(self.media_type_combo.currentText())

        return group

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

    def _build_launchers_group(self) -> QGroupBox:
        group = QGroupBox("Visualizers")

        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)

        for (label, sub, enabled), (row, col) in zip(
            [
                ("Metadata",    "metadata",    True),
                ("Shotlist",    "shotlist",    True),
                ("Mosaic",      "mosaic",      True),
                ("Cloud",       "cloud",       True),
                ("Book",        "book",        True),
                ("Silhouette",  "silhouette",  True),
                ("Palette",     "palette",     True),
                ("Flipbook",    "flipbook",    True),
            ],
            [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1)],
        ):
            btn = QPushButton(label)
            btn.setEnabled(enabled)
            if enabled:
                btn.clicked.connect(lambda _, s=sub: self._launch(s))
            grid.addWidget(btn, row, col)

        outer = QVBoxLayout(group)
        outer.setContentsMargins(8, 12, 8, 8)
        outer.addWidget(grid_widget)

        return group

    def _launch(self, subcommand: str) -> None:
        if not _prefs.get("path"):
            QMessageBox.warning(self, "No Project", "Please set a project folder first.")
            return
        proc = self._procs.get(subcommand)
        if proc is not None and proc.poll() is None:
            # Already running — raise its window
            title = _VISUALIZER_TITLE.get(subcommand, subcommand.capitalize())
            raised = False
            for cmd in (
                ["wmctrl", "-a", title],
                ["xdotool", "search", "--name", title, "windowactivate", "--sync"],
            ):
                try:
                    subprocess.Popen(cmd)
                    raised = True
                    break
                except FileNotFoundError:
                    continue
            return
        self._procs[subcommand] = subprocess.Popen(
            [sys.executable, str(_CLI_PATH), "visualizer", subcommand]
        )

    # ------------------------------------------------------------------
    # Keyboard

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Q, Qt.Key_W) and event.modifiers() & Qt.ControlModifier:
            self.close()
            return
        super().keyPressEvent(event)


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
