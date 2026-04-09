"""Compose Visualizer — interactive preview for the one-layer tableau composer.

Launched via:
    crossing generate compose <query> --visualizer

The window has:
  TOP   — toolbar: query input, orientation selector, Compose / Export buttons
  CENTER — full-size preview of the selected tableau image
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Fix Qt plugin conflict with OpenCV — import PyQt5 before cv2
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QImage, QPixmap

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Background worker: search + frame extraction
# ---------------------------------------------------------------------------

class ComposeWorker(QThread):
    """Runs search_shots() + choose_background() + build_tableau() in a thread.

    Signals
    -------
    result_ready(pil_image, result_dict)
        Emitted once the tableau PIL Image is ready.
    error(message)
        Emitted on failure.
    """

    result_ready = pyqtSignal(object, dict)
    error        = pyqtSignal(str)

    def __init__(
        self,
        query: str,
        orientation: str,
        project_path: str,
        parent=None,
    ):
        super().__init__(parent)
        self.query        = query
        self.orientation  = orientation
        self.project_path = project_path

    def run(self) -> None:
        try:
            from services.search import search_shots
            from generators.compose import choose_background, build_tableau

            res = search_shots(
                query          = self.query,
                scopes         = None,
                field          = None,
                limit          = None,
                limit_per_item = None,
                use_all        = True,
                project_path   = self.project_path,
            )
            results = res.get("results", [])

            if not results:
                self.error.emit(f"No results for query: {self.query!r}")
                return

            result  = choose_background(results)
            tableau = build_tableau(result, self.project_path, self.orientation)
            self.result_ready.emit(tableau, result)

        except Exception as exc:
            import traceback
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------

_DARK_BG  = "#121212"
_PANEL_BG = "#1e1e1e"
_CTRL_BG  = "#232323"
_TEXT     = "#dddddd"
_TEXT_DIM = "#888888"

_STYLESHEET = f"""
QMainWindow, QWidget  {{ background: {_PANEL_BG}; color: {_TEXT}; }}
QLabel                {{ color: {_TEXT}; }}
QComboBox, QLineEdit  {{
    background: #2d2d2d; color: {_TEXT};
    border: 1px solid #555; padding: 4px 6px; border-radius: 3px;
}}
QComboBox::drop-down  {{ border: none; }}
QPushButton           {{
    background: #2d2d2d; color: {_TEXT};
    border: 1px solid #555; padding: 5px 12px; border-radius: 3px;
}}
QPushButton:hover     {{ background: #3a3a3a; }}
QPushButton:pressed   {{ background: #484848; }}
QPushButton:disabled  {{ color: #555; border-color: #3a3a3a; }}
QStatusBar            {{ background: #1a1a1a; color: {_TEXT_DIM}; }}
"""

_TOOLBAR_H = 48


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ComposeVisualizer(QMainWindow):
    """Minimal compose visualizer — search, pick one background, preview."""

    def __init__(self, project_path: str, initial_query: str = ""):
        super().__init__()
        self.project_path    = project_path
        self._worker: Optional[ComposeWorker] = None
        self._current_img    = None   # PIL Image
        self._current_result: Optional[dict] = None

        self.setWindowTitle("Crossing — Compose Visualizer")
        self.resize(1280, 800)
        self.setStyleSheet(_STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # -- Toolbar ----------------------------------------------------------
        toolbar = QWidget()
        toolbar.setFixedHeight(_TOOLBAR_H)
        toolbar.setStyleSheet(f"background: {_CTRL_BG}; border-bottom: 1px solid #333;")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(12, 6, 12, 6)
        tb.setSpacing(8)

        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("Search criteria (background layer)…")
        self.query_input.returnPressed.connect(self._on_compose)
        if initial_query:
            self.query_input.setText(initial_query)

        self.orientation_combo = QComboBox()
        self.orientation_combo.addItems(["portrait", "landscape"])
        self.orientation_combo.setFixedWidth(110)

        self.compose_btn = QPushButton("Compose")
        self.compose_btn.setFixedWidth(90)
        self.compose_btn.clicked.connect(self._on_compose)

        self.export_btn = QPushButton("Export")
        self.export_btn.setFixedWidth(80)
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._on_export)

        tb.addWidget(QLabel("Query:"))
        tb.addWidget(self.query_input, stretch=1)
        tb.addWidget(QLabel("Orientation:"))
        tb.addWidget(self.orientation_combo)
        tb.addWidget(self.compose_btn)
        tb.addWidget(self.export_btn)
        main_layout.addWidget(toolbar)

        # -- Image display ----------------------------------------------------
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_label.setStyleSheet(f"background: {_DARK_BG};")
        main_layout.addWidget(self.image_label, stretch=1)

        # -- Status bar -------------------------------------------------------
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Enter a search query and press Compose.")

        # Auto-compose if a query was supplied on launch
        if initial_query:
            self._on_compose()

    # -------------------------------------------------------------------------
    # Compose action

    def _on_compose(self) -> None:
        query = self.query_input.text().strip()
        if not query:
            self.status.showMessage("Please enter a search query.")
            return
        orientation = self.orientation_combo.currentText()

        if self._worker is not None and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait()

        self.compose_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.status.showMessage(f"Searching for {query!r}…")

        self._worker = ComposeWorker(query, orientation, self.project_path)
        self._worker.result_ready.connect(self._on_result_ready)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_result_ready(self, img, result: dict) -> None:
        self._current_img    = img
        self._current_result = result
        self._refresh_display()
        movie = result.get("movie_title", result.get("movie_id", ""))
        self.status.showMessage(f"Background: {movie}")
        self.compose_btn.setEnabled(True)
        self.export_btn.setEnabled(True)

    def _on_error(self, message: str) -> None:
        self.status.showMessage(f"Error: {message.splitlines()[0]}")
        self.compose_btn.setEnabled(True)

    # -------------------------------------------------------------------------
    # Export action

    def _on_export(self) -> None:
        if self._current_img is None:
            return
        from generators.compose import save_tableau
        query   = self.query_input.text().strip() or "compose"
        out_dir = Path(self.project_path) / "output" / "compositions"
        out     = save_tableau(self._current_img, query, out_dir)
        self.status.showMessage(f"Saved: {out}")

    # -------------------------------------------------------------------------
    # Display helper

    def _refresh_display(self) -> None:
        """Re-scale and display _current_img in the image label."""
        if self._current_img is None:
            return
        img = self._current_img
        rgb    = img.tobytes("raw", "RGB")
        w, h   = img.size
        qimg   = QImage(rgb, w, h, 3 * w, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        label_size = self.image_label.size()
        scaled = pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_display()


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str, initial_query: str = "") -> None:
    """Create the QApplication (if needed) and launch the visualizer window."""
    app = QApplication.instance() or QApplication(sys.argv)
    win = ComposeVisualizer(project_path, initial_query=initial_query)
    win.show()
    sys.exit(app.exec_())
