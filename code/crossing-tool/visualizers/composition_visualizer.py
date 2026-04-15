"""Composition Visualizer — interactive preview for the one-layer tableau composer.

Launched via:
    crossing generate composition --visualizer

Layout:
  LEFT  — full-bleed image preview (fills remaining width)
  RIGHT — control panel: 4 layer keyword inputs, orientation, Compose / Export buttons
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Allow imports from the tool root (data/, services/, generators/)
sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme

# Fix Qt plugin conflict with OpenCV — import PyQt5 before cv2
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
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
from PyQt5.QtGui import QFont, QImage, QPixmap

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
            from generators.composition import choose_background, build_tableau

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
# Layout constants
# ---------------------------------------------------------------------------

_PANEL_W   = 260


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ComposeVisualizer(QMainWindow):
    """Compose visualizer — image fills the left, layer controls on the right."""

    def __init__(self, project_path: str, initial_query: str = ""):
        super().__init__()
        self.project_path    = project_path
        self._worker: Optional[ComposeWorker] = None
        self._current_img    = None   # PIL Image
        self._current_result: Optional[dict] = None

        self.setWindowTitle("Crossing — Composition Visualizer")
        self.resize(1440, 900)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- Left: image display ----------------------------------------------
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_label.setStyleSheet(f"background: {theme.CANVAS_BG};")
        root.addWidget(self.image_label, stretch=1)

        # -- Divider ----------------------------------------------------------
        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setFrameShadow(QFrame.Plain)
        divider.setFixedWidth(1)
        divider.setStyleSheet(f"background: {theme.UI_BORDER};")
        root.addWidget(divider)

        # -- Right: control panel ---------------------------------------------
        right_panel = QWidget()
        right_panel.setFixedWidth(_PANEL_W)
        right_panel.setStyleSheet(f"background: {theme.PANEL_BG};")
        rp = QVBoxLayout(right_panel)
        rp.setContentsMargins(14, 18, 14, 14)
        rp.setSpacing(0)
        root.addWidget(right_panel)

        label_font = theme.font_ui()
        label_font.setCapitalization(QFont.AllUppercase)
        label_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.2)

        dim_style   = f"color: {theme.TEXT_DIM};"

        def _add_layer(title: str, placeholder: str) -> QLineEdit:
            """Add a labelled layer group and return its QLineEdit."""
            lbl = QLabel(title)
            lbl.setFont(label_font)
            lbl.setStyleSheet(f"color: {theme.TEXT}; padding-bottom: 4px;")
            rp.addWidget(lbl)
            inp = QLineEdit()
            inp.setPlaceholderText(placeholder)
            inp.returnPressed.connect(self._on_compose)
            rp.addWidget(inp)
            rp.addSpacing(14)
            return inp

        self.bg_input     = _add_layer("Background",  "keyword…")
        self.middle_input = _add_layer("Middle",       "keyword…")
        self.mask_input   = _add_layer("Mask",         "keyword…")
        self.text_input   = _add_layer("Text",         "keyword…")

        # -- Separator --------------------------------------------------------
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Plain)
        sep.setStyleSheet(f"background: {theme.UI_BORDER};")
        rp.addWidget(sep)
        rp.addSpacing(14)

        # -- Orientation --
        ori_lbl = QLabel("Orientation")
        ori_lbl.setFont(label_font)
        ori_lbl.setStyleSheet(f"color: {theme.TEXT}; padding-bottom: 4px;")
        rp.addWidget(ori_lbl)

        self.orientation_combo = QComboBox()
        self.orientation_combo.addItems(["portrait", "landscape"])
        rp.addWidget(self.orientation_combo)

        # -- Push buttons to bottom -------------------------------------------
        rp.addStretch(1)

        # -- Action buttons ---------------------------------------------------
        self.compose_btn = QPushButton("Compose")
        self.compose_btn.clicked.connect(self._on_compose)
        rp.addWidget(self.compose_btn)
        rp.addSpacing(6)

        self.export_btn = QPushButton("Export")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._on_export)
        rp.addWidget(self.export_btn)

        # -- Status bar -------------------------------------------------------
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Enter a background keyword and press Compose.")

        # Seed initial query into background field
        if initial_query:
            self.bg_input.setText(initial_query)
            self._on_compose()

    # -------------------------------------------------------------------------
    # Compose action

    def _on_compose(self) -> None:
        query = self.bg_input.text().strip()
        if not query:
            self.status.showMessage("Please enter a background keyword.")
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
        from generators.composition import save_tableau
        query   = self.bg_input.text().strip() or "composition"
        out_dir = Path(self.project_path) / "output" / "compositions"
        out     = save_tableau(self._current_img, query, out_dir)
        self.status.showMessage(f"Saved: {out}")

    # -------------------------------------------------------------------------
    # Display helper

    def _refresh_display(self) -> None:
        """Re-scale and display _current_img in the image label."""
        if self._current_img is None:
            return
        img    = self._current_img
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

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Q, Qt.Key_W) and event.modifiers() & Qt.ControlModifier:
            self.close()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str, initial_query: str = "") -> None:
    """Create the QApplication (if needed) and launch the visualizer window."""
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    win = ComposeVisualizer(project_path, initial_query=initial_query)
    win.show()
    sys.exit(app.exec_())
