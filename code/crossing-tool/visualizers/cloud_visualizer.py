#!/usr/bin/env python3
"""Cloud Visualizer — interactive word-cloud explorer built from annotation text.

Launched via:
    crossing generate cloud --visualizer

Layout (canonical WindowVisualizer shell):
  BROWSER   — cloud canvas (rendered PDF page displayed as an image)
  INSPECTOR — Scope, Field, Options, Page Ratio and Actions sections
              (Generate / Save PDF buttons, status)

Keyboard:
  Home          — previous title in list
  End           — next title in list
  PgUp / PgDn   — previous / next annotation field
  Tab           — show/hide inspector
  Shift+Tab     — toggle fullscreen
  Escape / Ctrl+Q / Ctrl+W — close
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

# Allow imports from the tool root (data/, services/, generators/)
sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from tool.shortcuts import KEY_PREV_TITLE, KEY_NEXT_TITLE, KEY_PREV_ITEM, KEY_NEXT_ITEM
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.inspector import Inspector
from visualizers.components.tab_panel import TabPanel
from visualizers.components.combo_popup import add_combo_all_item, style_canonical_combo

from PyQt5.QtCore import Qt, QThread, QEvent, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from visualizers.components.sweep_bar import SweepBar

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Style colour editor dialog
# ---------------------------------------------------------------------------

class StyleEditorDialog(QDialog):
    """Modal dialog for viewing and editing a file-backed cloud style preset.

    Displays colour swatches for the background and each palette entry.
    Clicking a swatch opens a colour picker.  *Save & Apply* writes the
    changes back to the JSON file and reloads the style registry so the
    next Generate run uses the updated colours.
    """

    def __init__(self, style_name: str, parent=None):
        super().__init__(parent)
        from generators.cloud import get_style_path
        style_path = get_style_path(style_name)
        if style_path is None:
            raise ValueError(f"Style '{style_name}' has no backing JSON file.")
        self._style_name = style_name
        self._path       = style_path
        self._raw: dict  = json.loads(self._path.read_text())

        # Parse current colour values into mutable state
        bg_raw = self._raw.get("background", [18, 18, 18])
        self._bg_rgb: list = list(bg_raw["rgb"] if isinstance(bg_raw, dict) else bg_raw)

        self._entries: list[dict] = []
        for item in self._raw.get("palette", []):
            if isinstance(item, dict):
                self._entries.append({"label": item.get("label", ""), "rgb": list(item["rgb"])})
            else:
                self._entries.append({"label": "", "rgb": list(item)})

        # ── Layout ───────────────────────────────────────────────────────────
        self.setWindowTitle(f"Edit style: {style_name}")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        hdr_font = theme.font_ui()
        hdr_font.setCapitalization(QFont.AllUppercase)
        hdr_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.2)

        def _hdr(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setFont(hdr_font)
            lbl.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
            return lbl

        # Description (read-only)
        desc = self._raw.get("description", "")
        if desc:
            desc_lbl = QLabel(desc)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;"
            )
            layout.addWidget(desc_lbl)
            layout.addSpacing(4)

        # ── Background ───────────────────────────────────────────────────────
        layout.addWidget(_hdr("Background"))
        self._bg_btn = self._make_swatch(self._bg_rgb)
        self._bg_btn.clicked.connect(self._pick_bg)
        self._bg_lbl = QLabel(self._fmt(self._bg_rgb))
        self._bg_lbl.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;"
        )
        bg_row = QHBoxLayout()
        bg_row.setSpacing(8)
        bg_row.addWidget(self._bg_btn)
        bg_row.addWidget(self._bg_lbl)
        bg_row.addStretch()
        layout.addLayout(bg_row)

        layout.addSpacing(6)

        # ── Palette ──────────────────────────────────────────────────────────
        layout.addWidget(_hdr("Palette  ·  click a swatch to change  ·  duplicates = higher weight"))

        self._swatch_btns: list[QPushButton] = []
        self._rgb_labels:  list[QLabel]      = []

        inner = QWidget()
        grid  = QVBoxLayout(inner)
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setSpacing(4)

        for i, entry in enumerate(self._entries):
            row_w = QWidget()
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.setSpacing(8)

            sw = self._make_swatch(entry["rgb"])
            sw.clicked.connect(lambda _checked, idx=i: self._pick_palette(idx))

            name_lbl = QLabel(entry["label"] or "(no label)")
            name_lbl.setFixedWidth(130)
            name_lbl.setStyleSheet(
                f"color: {theme.TEXT}; font-size: 11px; background: transparent;"
            )
            rgb_lbl = QLabel(self._fmt(entry["rgb"]))
            rgb_lbl.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;"
            )

            row_l.addWidget(sw)
            row_l.addWidget(name_lbl)
            row_l.addWidget(rgb_lbl)
            row_l.addStretch()
            grid.addWidget(row_w)

            self._swatch_btns.append(sw)
            self._rgb_labels.append(rgb_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBar(theme.JumpScrollBar())
        scroll.setWidget(inner)
        scroll.setMinimumHeight(min(280, len(self._entries) * 34 + 20))
        layout.addWidget(scroll)

        layout.addSpacing(8)

        # ── Buttons ──────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("CANCEL")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("SAVE & APPLY")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch()
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt(rgb: list) -> str:
        return f"rgb({rgb[0]}, {rgb[1]}, {rgb[2]})"

    @staticmethod
    def _make_swatch(rgb: list) -> "QPushButton":
        btn = QPushButton()
        btn.setStyleSheet(
            f"background-color: rgb({rgb[0]},{rgb[1]},{rgb[2]});"
            f" border: 1px solid {theme.UI_BORDER};"
            f" min-width: 28px; max-width: 28px;"
            f" min-height: 20px; max-height: 20px;"
        )
        return btn

    @staticmethod
    def _update_swatch(btn: "QPushButton", rgb: list) -> None:
        btn.setStyleSheet(
            f"background-color: rgb({rgb[0]},{rgb[1]},{rgb[2]});"
            f" border: 1px solid {theme.UI_BORDER};"
            f" min-width: 28px; max-width: 28px;"
            f" min-height: 20px; max-height: 20px;"
        )

    def _pick_bg(self) -> None:
        c = QColorDialog.getColor(QColor(*self._bg_rgb), self, "Background colour")
        if c.isValid():
            self._bg_rgb = [c.red(), c.green(), c.blue()]
            self._update_swatch(self._bg_btn, self._bg_rgb)
            self._bg_lbl.setText(self._fmt(self._bg_rgb))

    def _pick_palette(self, idx: int) -> None:
        cur = self._entries[idx]["rgb"]
        c = QColorDialog.getColor(QColor(*cur), self, f"Palette colour {idx + 1}")
        if c.isValid():
            self._entries[idx]["rgb"] = [c.red(), c.green(), c.blue()]
            self._update_swatch(self._swatch_btns[idx], self._entries[idx]["rgb"])
            self._rgb_labels[idx].setText(self._fmt(self._entries[idx]["rgb"]))

    def _save(self) -> None:
        raw = dict(self._raw)  # shallow copy — preserves description, color_model, etc.

        # Write background
        bg_orig = raw.get("background")
        if isinstance(bg_orig, dict):
            updated_bg = dict(bg_orig)
            updated_bg["rgb"] = self._bg_rgb
            raw["background"] = updated_bg
        else:
            raw["background"] = self._bg_rgb

        # Write palette
        old_palette = raw.get("palette", [])
        new_palette = []
        for i, entry in enumerate(self._entries):
            orig = old_palette[i] if i < len(old_palette) else {}
            if isinstance(orig, dict):
                updated = dict(orig)
                updated["rgb"] = entry["rgb"]
                new_palette.append(updated)
            else:
                new_palette.append(entry["rgb"])
        raw["palette"] = new_palette

        self._path.write_text(json.dumps(raw, indent=2))

        from generators.cloud import reload_styles
        reload_styles()
        self.accept()


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
        width: int = 1200,
        height: int = 840,
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
        self.width        = width
        self.height       = height

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
                width=self.width,
                height=self.height,
            )
            self.result_ready.emit(canvas.convert("RGB"), str(out))
        except Exception as exc:
            import traceback
            self.error.emit(f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Adaptive canvas widget
# ---------------------------------------------------------------------------

class _CloudCanvas(QWidget):
    """Displays the generated word-cloud image scaled to fit available space.

    A plain `QLabel.setPixmap()` reports a sizeHint/minimumSizeHint equal to
    whatever pixmap was last set on it — once a GripSplitter pane uses that
    as its floor, the window can grow (each resize sets a larger pixmap,
    raising the floor further) but can never shrink back below it. This
    widget instead paints its own content on demand (mirroring `_SpreadView`
    in `book_visualizer.py`) and never reports a size hint tied to the
    image, so it can always be freely shrunk or grown by the splitter. The
    source pixmap is kept at native resolution and only ever scaled to fit
    the widget's *current* size at paint time, preserving aspect ratio and
    centered (letterboxed).
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._source: Optional[QPixmap] = None   # native-resolution image
        self._scaled: Optional[QPixmap] = None   # cached fit-to-current-size scan
        self._scaled_size = None
        self._placeholder = "No cloud generated yet."

    def set_image(self, pixmap: QPixmap) -> None:
        """Set the native-resolution source image and repaint."""
        self._source = pixmap
        self._scaled = None
        self._scaled_size = None
        self.update()

    def clear(self) -> None:
        self._source = None
        self._scaled = None
        self._scaled_size = None
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            p.fillRect(self.rect(), QColor(theme.CANVAS_BG))
            if self._source is None:
                p.setPen(QColor(theme.TEXT_DIM))
                p.drawText(self.rect(), Qt.AlignCenter, self._placeholder)
                return
            size = self.size()
            if self._scaled is None or self._scaled_size != size:
                self._scaled = self._source.scaled(
                    size, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                self._scaled_size = size
            x = (self.width()  - self._scaled.width())  // 2
            y = (self.height() - self._scaled.height()) // 2
            p.drawPixmap(x, y, self._scaled)
        finally:
            p.end()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class CloudVisualizer(WindowVisualizer):
    """Word-cloud visualizer — canvas browser, controls in the inspector."""

    def __init__(self, project_path: str) -> None:
        # Instance attributes must be set before super().__init__() since the
        # base class calls create_browser()/create_inspector() synchronously.
        self.project_path  = project_path
        self._worker: Optional[CloudWorker] = None
        self._current_img  = None     # PIL Image
        self._current_path: Optional[str] = None  # last saved path

        super().__init__(pref_key="window_cloud")
        self.setWindowTitle("Cloud")
        self.resize(1400, 900)

        self._populate_movies()

    def create_browser(self) -> QWidget:
        self.canvas = _CloudCanvas()
        return self.canvas

    def create_inspector(self) -> QWidget:
        panel = TabPanel()

        # ── Scope section ────────────────────────────────────────────
        scope_wrap = QWidget()
        scope_layout = QVBoxLayout(scope_wrap)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_layout.setSpacing(theme.SECTION_GAP)
        self.media_combo = QComboBox()
        self.media_combo.addItem("<All Media>", userData="--all")
        self.media_combo.addItem("movie", userData="movie")
        self.media_combo.addItem("gameplay", userData="gameplay")
        self.media_combo.setCurrentIndex(1)
        style_canonical_combo(self.media_combo)
        self.media_combo.currentIndexChanged.connect(self._populate_movies)
        scope_layout.addWidget(self.media_combo)
        self.movie_combo = QComboBox()
        add_combo_all_item(self.movie_combo)
        self.movie_combo.setItemText(0, "<All Titles>")
        style_canonical_combo(self.movie_combo)
        self.movie_combo.installEventFilter(self)
        scope_layout.addWidget(self.movie_combo)
        panel.add_section("Scope", scope_wrap, pref_key="cloud_section_scope")

        # ── Field section ────────────────────────────────────────────
        field_wrap = QWidget()
        field_layout = QVBoxLayout(field_wrap)
        field_layout.setContentsMargins(0, 0, 0, 0)
        field_layout.setSpacing(theme.SECTION_GAP)
        self.field_combo = QComboBox()
        self.field_combo.addItem("<All Fields>", userData=None)
        for f in (
            "setting", "description", "objects", "action",
            "humans", "wearing", "animals", "text", "motif",
        ):
            self.field_combo.addItem(f, userData=f)
        style_canonical_combo(self.field_combo)
        self.field_combo.installEventFilter(self)
        field_layout.addWidget(self.field_combo)
        panel.add_section("Field", field_wrap, pref_key="cloud_section_field")

        # ── Options section ──────────────────────────────────────────
        opt_wrap = QWidget()
        opt_layout = QVBoxLayout(opt_wrap)
        opt_layout.setContentsMargins(0, 0, 0, 0)
        opt_layout.setSpacing(theme.SECTION_GAP)

        max_lbl = QLabel("Max words")
        max_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        opt_layout.addWidget(max_lbl)
        self.max_words_spin = QSpinBox()
        self.max_words_spin.setRange(10, 500)
        self.max_words_spin.setValue(150)
        opt_layout.addWidget(self.max_words_spin)

        min_lbl = QLabel("Min occurrences")
        min_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        opt_layout.addWidget(min_lbl)
        self.min_count_spin = QSpinBox()
        self.min_count_spin.setRange(1, 100)
        self.min_count_spin.setValue(2)
        opt_layout.addWidget(self.min_count_spin)

        panel.add_section("Options", opt_wrap, pref_key="cloud_section_options")

        # ── Styles section ───────────────────────────────────────────
        style_wrap = QWidget()
        style_layout = QVBoxLayout(style_wrap)
        style_layout.setContentsMargins(0, 0, 0, 0)
        style_layout.setSpacing(theme.SECTION_GAP)

        self.style_combo = QComboBox()
        from generators.cloud import STYLE_NAMES, PREFS_KEY_STYLE, DEFAULT_STYLE
        for name in STYLE_NAMES:
            self.style_combo.addItem(name, userData=name)
        style_canonical_combo(self.style_combo)
        from tool import prefs as _prefs
        saved_style = _prefs.get(PREFS_KEY_STYLE) or DEFAULT_STYLE
        idx = self.style_combo.findData(saved_style)
        if idx >= 0:
            self.style_combo.setCurrentIndex(idx)
        self.style_combo.currentIndexChanged.connect(self._on_style_changed)
        self.style_combo.currentIndexChanged.connect(self._update_style_buttons)
        style_layout.addWidget(self.style_combo)

        btn_grid = QGridLayout()
        btn_grid.setContentsMargins(0, 0, 0, 0)
        btn_grid.setSpacing(theme.SECTION_GAP)

        self.new_style_btn = QPushButton("New")
        self.new_style_btn.setStyleSheet(theme.action_button_stylesheet())
        self.new_style_btn.setFocusPolicy(Qt.NoFocus)
        self.new_style_btn.clicked.connect(self._on_new_style)
        btn_grid.addWidget(self.new_style_btn, 0, 0)

        self.duplicate_style_btn = QPushButton("Duplicate")
        self.duplicate_style_btn.setStyleSheet(theme.action_button_stylesheet())
        self.duplicate_style_btn.setFocusPolicy(Qt.NoFocus)
        self.duplicate_style_btn.clicked.connect(self._on_duplicate_style)
        btn_grid.addWidget(self.duplicate_style_btn, 0, 1)

        self.rename_style_btn = QPushButton("Rename")
        self.rename_style_btn.setStyleSheet(theme.action_button_stylesheet())
        self.rename_style_btn.setFocusPolicy(Qt.NoFocus)
        self.rename_style_btn.clicked.connect(self._on_rename_style)
        btn_grid.addWidget(self.rename_style_btn, 1, 0)

        self.delete_style_btn = QPushButton("Delete")
        self.delete_style_btn.setStyleSheet(theme.action_button_stylesheet())
        self.delete_style_btn.setFocusPolicy(Qt.NoFocus)
        self.delete_style_btn.clicked.connect(self._on_delete_style)
        btn_grid.addWidget(self.delete_style_btn, 1, 1)

        self.edit_colors_btn = QPushButton("Edit Colors")
        self.edit_colors_btn.setStyleSheet(theme.action_button_stylesheet())
        self.edit_colors_btn.setFocusPolicy(Qt.NoFocus)
        self.edit_colors_btn.clicked.connect(self._on_edit_colors)
        btn_grid.addWidget(self.edit_colors_btn, 2, 0, 1, 2)

        style_layout.addLayout(btn_grid)

        self._update_style_buttons()
        panel.add_section("Styles", style_wrap, pref_key="cloud_section_styles")

        # ── Page Ratio section ───────────────────────────────────────
        ratio_wrap = QWidget()
        ratio_layout = QVBoxLayout(ratio_wrap)
        ratio_layout.setContentsMargins(0, 0, 0, 0)
        ratio_layout.setSpacing(theme.SECTION_GAP)

        self.ratio_combo = QComboBox()
        self.ratio_combo.addItem("16:9",  userData=(16, 9))
        self.ratio_combo.addItem("2:3",   userData=(2, 3))
        self.ratio_combo.addItem("Custom", userData=None)
        style_canonical_combo(self.ratio_combo)
        self.ratio_combo.currentIndexChanged.connect(self._on_ratio_changed)
        ratio_layout.addWidget(self.ratio_combo)

        custom_row = QHBoxLayout()
        custom_row.setSpacing(4)
        w_lbl = QLabel("W")
        w_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        custom_row.addWidget(w_lbl)
        self.ratio_w_edit = QLineEdit("16")
        self.ratio_w_edit.setFixedWidth(38)
        custom_row.addWidget(self.ratio_w_edit)
        h_lbl = QLabel("H")
        h_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        custom_row.addWidget(h_lbl)
        self.ratio_h_edit = QLineEdit("9")
        self.ratio_h_edit.setFixedWidth(38)
        custom_row.addWidget(self.ratio_h_edit)
        custom_row.addStretch(1)
        ratio_layout.addLayout(custom_row)

        self._on_ratio_changed(0)   # set initial enabled state
        panel.add_section("Page Ratio", ratio_wrap, pref_key="cloud_section_ratio")

        # ── Tools section ──────────────────────────────────────────
        actions_wrap = QWidget()
        actions_layout = QVBoxLayout(actions_wrap)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(theme.SECTION_GAP)

        self.generate_btn = QPushButton("Generate")
        self.generate_btn.setStyleSheet(theme.action_button_stylesheet())
        self.generate_btn.setFocusPolicy(Qt.NoFocus)
        self.generate_btn.clicked.connect(self._on_generate)
        actions_layout.addWidget(self.generate_btn)

        self.save_btn = QPushButton("Save PDF")
        self.save_btn.setStyleSheet(theme.action_button_stylesheet())
        self.save_btn.setFocusPolicy(Qt.NoFocus)
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._on_save_pdf)
        actions_layout.addWidget(self.save_btn)

        tools_sec = panel.add_section("Tools", actions_wrap, pref_key="cloud_section_tools")

        # Accent sweep-bar shown on the section title while Generate is
        # running in the background — same loading behavior as Illustration's
        # Silhouettes/Engravings tabs and Palette's Movie section.
        self._loading_bar = SweepBar(self)
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(20)   # ~50 fps
        self._loading_timer.timeout.connect(self._loading_bar.tick)
        tools_sec.set_subbar(self._loading_bar)

        self._inspector = Inspector()
        self._inspector.add_tab(panel, "Cloud")
        return self._inspector

    # ── Generate ─────────────────────────────────────────────────────────────

    def _on_ratio_changed(self, _index: int) -> None:
        """Enable/disable the custom W/H fields based on combo selection."""
        is_custom = self.ratio_combo.currentData() is None
        self.ratio_w_edit.setEnabled(is_custom)
        self.ratio_h_edit.setEnabled(is_custom)

    def _resolve_dimensions(self) -> tuple[int, int]:
        """Return (width, height) in pixels for the selected page ratio.

        The long side is always 1200 px; the short side is scaled accordingly.
        For custom ratios the user-supplied W:H values are used directly.
        """
        ratio = self.ratio_combo.currentData()
        if ratio is not None:
            rw, rh = ratio
        else:
            try:
                rw = max(1, int(self.ratio_w_edit.text()))
            except ValueError:
                rw = 16
            try:
                rh = max(1, int(self.ratio_h_edit.text()))
            except ValueError:
                rh = 9
        # Keep long side at 1200 px
        if rw >= rh:
            w = 1200
            h = round(1200 * rh / rw)
        else:
            h = 1200
            w = round(1200 * rw / rh)
        return w, h

    def _on_generate(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait()

        scope_data = self.movie_combo.currentData()
        scope      = scope_data[1] if isinstance(scope_data, tuple) else scope_data or ""
        field      = self.field_combo.currentData()
        media_type = scope_data[0] if isinstance(scope_data, tuple) else self.media_combo.currentData()
        max_words  = self.max_words_spin.value()
        min_count  = self.min_count_spin.value()
        style      = self.style_combo.currentData() or "default"
        width, height = self._resolve_dimensions()

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
        self._loading_bar.start()
        self._loading_timer.start()

        self._worker = CloudWorker(
            self.project_path,
            scope,
            field,
            media_type,
            max_words,
            min_count,
            str(out_path),
            style,
            width=width,
            height=height,
        )
        self._worker.result_ready.connect(self._on_result_ready)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    # ── Movie list ────────────────────────────────────────────────────────────

    def _on_style_changed(self, _index: int) -> None:
        """Persist the selected style as the project default."""
        from tool import prefs as _prefs
        from generators.cloud import PREFS_KEY_STYLE
        _prefs.set(PREFS_KEY_STYLE, self.style_combo.currentData())

    def _update_style_buttons(self, _index: int = 0) -> None:
        """Enable Edit Colors/Rename/Delete only when a file-backed
        (non-default) style is active; New and Duplicate are always
        available."""
        from generators.cloud import get_style_path
        style = self.style_combo.currentData()
        has_file = bool(style and get_style_path(style) is not None)
        self.edit_colors_btn.setEnabled(has_file)
        self.rename_style_btn.setEnabled(has_file)
        self.delete_style_btn.setEnabled(has_file)

    def _reload_style_combo(self, select_name: Optional[str] = None) -> None:
        """Repopulate style_combo from the (already reloaded) style registry.

        Selects `select_name` if given, else keeps the previous selection if
        it still exists, else falls back to "default".
        """
        from generators.cloud import STYLE_NAMES, DEFAULT_STYLE
        keep = select_name or self.style_combo.currentData() or DEFAULT_STYLE
        self.style_combo.blockSignals(True)
        self.style_combo.clear()
        for name in STYLE_NAMES:
            self.style_combo.addItem(name, userData=name)
        idx = self.style_combo.findData(keep)
        if idx < 0:
            idx = self.style_combo.findData(DEFAULT_STYLE)
        if idx >= 0:
            self.style_combo.setCurrentIndex(idx)
        self.style_combo.blockSignals(False)
        self._on_style_changed(self.style_combo.currentIndex())
        self._update_style_buttons()

    def _on_new_style(self) -> None:
        """Create a new style preset, seeded from the built-in default colours."""
        name, ok = QInputDialog.getText(self, "New Style", "Style name:")
        if not ok:
            return
        try:
            from generators.cloud import create_style
            create_style(name, base_style="default")
        except Exception as exc:
            QMessageBox.warning(self, "Cannot create style", str(exc))
            return
        self._reload_style_combo(select_name=name.strip())

    def _on_duplicate_style(self) -> None:
        """Duplicate the currently selected style's colours under a new name."""
        base = self.style_combo.currentData() or "default"
        name, ok = QInputDialog.getText(self, "Duplicate Style", "New name:")
        if not ok:
            return
        try:
            from generators.cloud import create_style
            create_style(name, base_style=base)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot duplicate style", str(exc))
            return
        self._reload_style_combo(select_name=name.strip())

    def _on_rename_style(self) -> None:
        """Rename the currently selected (non-default) style preset."""
        old_name = self.style_combo.currentData()
        if not old_name:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename Style", "New name:", text=old_name
        )
        if not ok:
            return
        try:
            from generators.cloud import rename_style
            rename_style(old_name, new_name)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot rename style", str(exc))
            return
        self._reload_style_combo(select_name=new_name.strip())

    def _on_delete_style(self) -> None:
        """Delete the currently selected (non-default) style preset."""
        name = self.style_combo.currentData()
        if not name:
            return
        reply = QMessageBox.question(
            self,
            "Delete Style",
            f'Delete style "{name}"? This cannot be undone.',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from generators.cloud import delete_style
            delete_style(name)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot delete style", str(exc))
            return
        self._reload_style_combo(select_name="default")

    def _on_edit_colors(self) -> None:
        """Open the colour editor for the currently selected style."""
        style = self.style_combo.currentData()
        if not style:
            return
        try:
            dlg = StyleEditorDialog(style, parent=self)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot open editor", str(exc))
            return
        dlg.exec_()

    def _populate_movies(self) -> None:
        """Populate movie_combo from project metadata for the selected media type."""
        media_type = self.media_combo.currentData()
        self.movie_combo.blockSignals(True)
        self.movie_combo.clear()
        add_combo_all_item(self.movie_combo)
        self.movie_combo.setItemText(0, "<All Titles>")
        try:
            from data.metadata import get_metadata
            media_types = ("movie", "gameplay") if media_type == "--all" else (media_type,)
            rows = [
                (child_media_type, row)
                for child_media_type in media_types
                for row in get_metadata(self.project_path, media_type=child_media_type)
            ]
            sorted_rows = sorted(rows, key=lambda item: (item[1].get("title") or "").lower())
            for child_media_type, row in sorted_rows:
                title = row.get("title", "")
                year  = row.get("year", "")
                label = f"{title} ({year})" if year else title
                stem  = Path(row.get("filename", "")).stem
                if label and stem:
                    if media_type == "--all":
                        self.movie_combo.addItem(
                            f"{label} [{child_media_type}]",
                            userData=(child_media_type, stem),
                        )
                    else:
                        self.movie_combo.addItem(label, userData=stem)
        except Exception as exc:
            QMessageBox.warning(self, "Could not load movie list", str(exc))
        finally:
            self.movie_combo.blockSignals(False)

    def _on_result_ready(self, img, path: str) -> None:
        self._loading_timer.stop()
        self._loading_bar.stop()
        self._current_img  = img
        self._current_path = path
        self._refresh_display()
        self.generate_btn.setEnabled(True)
        self.save_btn.setEnabled(True)

    def _on_error(self, message: str) -> None:
        self._loading_timer.stop()
        self._loading_bar.stop()
        self.generate_btn.setEnabled(True)
        first_line = message.splitlines()[0]
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
        self.canvas.set_image(pixmap)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Intercept Home/End/PgUp/PgDn on combo boxes to override native
        QComboBox behaviour: Home/End step one title; PgUp/PgDn cycle field."""
        if event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Home, Qt.Key_End):
                # Always navigate titles, regardless of which combo has focus
                idx = self.movie_combo.currentIndex()
                if key == Qt.Key_Home:
                    if idx > 0:
                        self.movie_combo.setCurrentIndex(idx - 1)
                else:
                    if idx < self.movie_combo.count() - 1:
                        self.movie_combo.setCurrentIndex(idx + 1)
                return True
            if obj is self.movie_combo and key in (Qt.Key_PageUp, Qt.Key_PageDown):
                idx = self.field_combo.currentIndex()
                if key == Qt.Key_PageUp:
                    if idx > 0:
                        self.field_combo.setCurrentIndex(idx - 1)
                else:
                    if idx < self.field_combo.count() - 1:
                        self.field_combo.setCurrentIndex(idx + 1)
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == KEY_PREV_TITLE:
            idx = self.movie_combo.currentIndex()
            if idx > 0:
                self.movie_combo.setCurrentIndex(idx - 1)
        elif key == KEY_NEXT_TITLE:
            idx = self.movie_combo.currentIndex()
            if idx < self.movie_combo.count() - 1:
                self.movie_combo.setCurrentIndex(idx + 1)
        elif key == KEY_PREV_ITEM:
            idx = self.field_combo.currentIndex()
            if idx > 0:
                self.field_combo.setCurrentIndex(idx - 1)
        elif key == KEY_NEXT_ITEM:
            idx = self.field_combo.currentIndex()
            if idx < self.field_combo.count() - 1:
                self.field_combo.setCurrentIndex(idx + 1)
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str) -> None:
    """Create the QApplication (if needed) and launch the cloud visualizer."""
    from visualizers.launcher import run_visualizer_window
    run_visualizer_window("cloud", lambda: CloudVisualizer(project_path))


if __name__ == "__main__":
    from tool import prefs as _prefs
    _pp = _prefs.get("path") or "."
    run_visualizer(_pp)
