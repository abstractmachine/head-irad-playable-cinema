#!/usr/bin/env python3
"""Cloud Visualizer — interactive word-cloud explorer built from annotation text.

Launched via:
    crossing generate cloud --visualizer

Layout:
  LEFT  — cloud canvas (rendered PDF page displayed as an image)
  RIGHT — control panel: scope, field, options, Generate and Save PDF buttons
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Allow imports from the tool root (data/, services/, generators/)
sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import save_window_geometry, restore_window_geometry

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QFont, QImage, QPixmap

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class CloudWorker(QThread):
    """Runs cloud_from_annotations() in a worker thread.

    Signals
    -------
    result_ready(pil_image, output_path)
        Emitted when the cloud has been rendered and saved.
    error(message)
        Emitted on failure.
    """

    result_ready = pyqtSignal(object, str)   # (PIL Image, path string)
    error        = pyqtSignal(str)

    def __init__(
        self,
        project_path: str,
        scope: str,
        field: str,
        media_type: str,
        max_words: int,
        min_count: int,
        output_path: str,
        style: str = "default",
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = project_path
        self.scope        = scope or None
        self.field        = field or None
        self.media_type   = media_type
        self.max_words    = max_words
        self.min_count    = min_count
        self.output_path  = output_path
        self.style        = style

    def run(self) -> None:
        try:
            from generators.cloud import cloud_from_annotations
            out, canvas = cloud_from_annotations(
                self.project_path,
                scope=self.scope,
                field=self.field,
                media_type=self.media_type,
                output_path=self.output_path,
                max_words=self.max_words,
                min_count=self.min_count,
                style=self.style,
            )
            self.result_ready.emit(canvas.convert("RGB"), str(out))
        except Exception as exc:
            import traceback
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

_PANEL_W = 270


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class CloudVisualizer(QMainWindow):
    """Word-cloud visualizer — canvas on the left, controls on the right."""

    def __init__(self, project_path: str) -> None:
        super().__init__()
        self.project_path  = project_path
        self._worker: Optional[CloudWorker] = None
        self._current_img  = None     # PIL Image
        self._current_path: Optional[str] = None  # last saved path

        self.setWindowTitle("Crossing — Cloud Visualizer")
        self.resize(1400, 900)
        restore_window_geometry(self, "window_cloud")

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Left: cloud canvas ──────────────────────────────────────────────
        self.canvas_label = QLabel()
        self.canvas_label.setAlignment(Qt.AlignCenter)
        self.canvas_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.canvas_label.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self.canvas_label.setText("No cloud generated yet.")
        root.addWidget(self.canvas_label, stretch=1)

        # ── Divider ─────────────────────────────────────────────────────────
        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setFrameShadow(QFrame.Plain)
        divider.setFixedWidth(1)
        divider.setStyleSheet(f"background: {theme.UI_BORDER};")
        root.addWidget(divider)

        # ── Right: control panel ─────────────────────────────────────────────
        panel = QWidget()
        panel.setFixedWidth(_PANEL_W)
        panel.setStyleSheet(
            f"QWidget {{ background: {theme.PANEL_BG}; }}"
            f" QPushButton {{ background-color: {theme.BTN_BG}; border: none;"
            f" padding: 0 10px; border-radius: 3px;"
            f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; }}"
            f" QPushButton:hover    {{ background-color: {theme.BTN_HOVER}; }}"
            f" QPushButton:pressed  {{ background-color: {theme.BTN_PRESSED}; }}"
            f" QPushButton:disabled {{ color: {theme.TEXT_DIM};"
            f" background-color: {theme.BTN_BG}; }}"
        )
        rp = QVBoxLayout(panel)
        rp.setContentsMargins(14, 18, 14, 14)
        rp.setSpacing(0)
        root.addWidget(panel)

        label_font = theme.font_ui()
        label_font.setCapitalization(QFont.AllUppercase)
        label_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.2)

        def _section(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setFont(label_font)
            lbl.setStyleSheet(f"color: {theme.TEXT}; padding-bottom: 4px;")
            return lbl

        # Scope (movie dropdown)
        rp.addWidget(_section("Scope"))
        self.movie_combo = QComboBox()
        self.movie_combo.addItem("--all", userData=None)
        rp.addWidget(self.movie_combo)
        rp.addSpacing(12)

        # Annotation field
        rp.addWidget(_section("Field"))
        self.field_combo = QComboBox()
        self.field_combo.addItem("all fields", userData=None)
        for f in (
            "setting", "description", "objects", "action",
            "humans", "wearing", "animals", "text",
        ):
            self.field_combo.addItem(f, userData=f)
        rp.addWidget(self.field_combo)
        rp.addSpacing(12)

        # Media type
        rp.addWidget(_section("Media type"))
        self.media_combo = QComboBox()
        self.media_combo.addItems(["movies", "gameplay"])
        rp.addWidget(self.media_combo)
        rp.addSpacing(12)
        self.media_combo.currentIndexChanged.connect(self._populate_movies)

        # Max words
        rp.addWidget(_section("Max words"))
        self.max_words_spin = QSpinBox()
        self.max_words_spin.setRange(10, 500)
        self.max_words_spin.setValue(150)
        rp.addWidget(self.max_words_spin)
        rp.addSpacing(12)

        # Min count
        rp.addWidget(_section("Min occurrences"))
        self.min_count_spin = QSpinBox()
        self.min_count_spin.setRange(1, 100)
        self.min_count_spin.setValue(2)
        rp.addWidget(self.min_count_spin)
        rp.addSpacing(12)

        # Style preset
        rp.addWidget(_section("Style"))
        self.style_combo = QComboBox()
        from generators.cloud import STYLE_NAMES, PREFS_KEY_STYLE, DEFAULT_STYLE
        for name in STYLE_NAMES:
            self.style_combo.addItem(name, userData=name)
        saved_style = __import__("prefs").get(PREFS_KEY_STYLE) or DEFAULT_STYLE
        idx = self.style_combo.findData(saved_style)
        if idx >= 0:
            self.style_combo.setCurrentIndex(idx)
        self.style_combo.currentIndexChanged.connect(self._on_style_changed)
        rp.addWidget(self.style_combo)
        rp.addSpacing(18)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Plain)
        sep.setStyleSheet(f"background: {theme.UI_BORDER};")
        rp.addWidget(sep)
        rp.addSpacing(14)

        rp.addStretch(1)

        # Generate button
        self.generate_btn = QPushButton("GENERATE")
        self.generate_btn.clicked.connect(self._on_generate)
        rp.addWidget(self.generate_btn)
        rp.addSpacing(6)

        # Save PDF button (enabled after a successful generation)
        self.save_btn = QPushButton("SAVE PDF")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._on_save_pdf)
        rp.addWidget(self.save_btn)
        rp.addSpacing(10)

        # Status label
        self.status_label = QLabel("Choose options and press GENERATE.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;"
        )
        rp.addWidget(self.status_label)

        self._populate_movies()

    # ── Generate ─────────────────────────────────────────────────────────────

    def _on_generate(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait()

        scope      = self.movie_combo.currentData() or ""
        field      = self.field_combo.currentData()
        media_type = self.media_combo.currentText()
        max_words  = self.max_words_spin.value()
        min_count  = self.min_count_spin.value()
        style      = self.style_combo.currentData() or "default"

        # Generate into the project's output/clouds/ folder (temporary name)
        import datetime
        stamp      = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        import re
        scope_slug = re.sub(r"[^\w]+", "-", scope or media_type).strip("-")
        field_slug = re.sub(r"[^\w]+", "-", field or "all").strip("-")
        out_path   = (
            Path(self.project_path) / "output" / "clouds"
            / f"{scope_slug}-{field_slug}-cloud-{stamp}.pdf"
        )

        self.generate_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        scope_label = scope or f"{media_type} (all)"
        field_label = field or "all fields"
        self.status_label.setText(
            f"Generating cloud: {scope_label} · {field_label} [{style}]…"
        )

        self._worker = CloudWorker(
            self.project_path,
            scope,
            field,
            media_type,
            max_words,
            min_count,
            str(out_path),
            style,
        )
        self._worker.result_ready.connect(self._on_result_ready)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    # ── Movie list ────────────────────────────────────────────────────────────

    def _on_style_changed(self, _index: int) -> None:
        """Persist the selected style as the project default."""
        import prefs as _prefs
        from generators.cloud import PREFS_KEY_STYLE
        _prefs.set(PREFS_KEY_STYLE, self.style_combo.currentData())

    def _populate_movies(self) -> None:
        """Populate movie_combo from project metadata for the selected media type."""
        media_type = self.media_combo.currentText()
        self.movie_combo.blockSignals(True)
        self.movie_combo.clear()
        self.movie_combo.addItem("--all", userData=None)
        try:
            from data.metadata import get_metadata
            rows = get_metadata(self.project_path, media_type=media_type)
            sorted_rows = sorted(rows, key=lambda r: (r.get("title") or "").lower())
            for row in sorted_rows:
                title = row.get("title", "")
                year  = row.get("year", "")
                label = f"{title} ({year})" if year else title
                stem  = Path(row.get("filename", "")).stem
                if label and stem:
                    self.movie_combo.addItem(label, userData=stem)
        except Exception as exc:
            self.status_label.setText(f"Warning: could not load movie list — {exc}")
        finally:
            self.movie_combo.blockSignals(False)

    def _on_result_ready(self, img, path: str) -> None:
        self._current_img  = img
        self._current_path = path
        self._refresh_display()
        self.generate_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.status_label.setText(f"Cloud ready — saved to {Path(path).name}")

    def _on_error(self, message: str) -> None:
        self.generate_btn.setEnabled(True)
        first_line = message.splitlines()[0]
        self.status_label.setText(f"Error: {first_line}")
        QMessageBox.warning(self, "Cloud generation failed", first_line)

    # ── Save PDF ──────────────────────────────────────────────────────────────

    def _on_save_pdf(self) -> None:
        """Save the current cloud PDF to a user-chosen location."""
        if self._current_path is None:
            return

        # Suggest a default filename derived from the last generated path
        default_name = Path(self._current_path).name

        dest, _ = QFileDialog.getSaveFileName(
            self,
            "Save Cloud as PDF",
            str(Path.home() / default_name),
            "PDF files (*.pdf);;All files (*)",
        )
        if not dest:
            return  # user cancelled

        if not dest.lower().endswith(".pdf"):
            dest += ".pdf"

        try:
            import shutil
            shutil.copy2(self._current_path, dest)
            self.status_label.setText(f"Saved: {dest}")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    # ── Display helpers ────────────────────────────────────────────────────────

    def _refresh_display(self) -> None:
        if self._current_img is None:
            return
        img = self._current_img
        rgb = img.tobytes("raw", "RGB")
        w, h = img.size
        qimg = QImage(rgb, w, h, 3 * w, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        label_size = self.canvas_label.size()
        scaled = pixmap.scaled(
            label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.canvas_label.setPixmap(scaled)
        self.canvas_label.setText("")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_display()

    def closeEvent(self, event) -> None:
        save_window_geometry(self, "window_cloud")
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        if (
            event.key() in (Qt.Key_Q, Qt.Key_W)
            and event.modifiers() & Qt.ControlModifier
        ):
            self.close()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str) -> None:
    """Create the QApplication (if needed) and launch the cloud visualizer."""
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    win = CloudVisualizer(project_path)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    import prefs as _prefs
    _pp = _prefs.get("path") or "."
    run_visualizer(_pp)
