#!/usr/bin/env python3
"""Flipbook Visualizer — browse cinematic motif flipbook pages.

Launched via:
    crossing visualizer flipbook
    crossing visualizer flipbook --media gameplay

Layout:
  TOP  — movie selector dropdown + page-count status label + Export PDF button
  MAIN — scrollable grid of page thumbnails (bg fill + motif word)

Each page thumbnail is:
  - filled with the shot's palette background color
  - motif word centered in the shot's palette foreground color
  - typed in Libre Clarendon Normal Medium

Covers (front and back) are included as the first and last pages.

Keyboard:
  Home          — previous title
  End           — next title
  Escape / Ctrl+Q / Ctrl+W — close
  Ctrl+P        — export PDF for current movie
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import save_window_geometry, restore_window_geometry

from PyQt5.QtCore import Qt, QEvent, QSize
from tool.shortcuts import KEY_PREV_TITLE, KEY_NEXT_TITLE
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QPainter

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ASPECT = 16 / 9      # page width : height ratio  (cinema widescreen)
_GAP    = 6           # px — gap between page thumbnails
_MARGIN = 12          # px — grid outer margin


# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------

_FONTS_DIR   = Path(__file__).parent.parent / "styles" / "fonts"
_LC_DIR      = _FONTS_DIR / "libre_clarendon" / "fonts"
_FONT_FAMILY = None  # populated by _load_flipbook_font() below


def _load_flipbook_font() -> str:
    """Load Libre Clarendon variants into QFontDatabase and return the family name.

    Falls back to the system default if the font files are not found.
    Called once at module import time and cached in _FONT_FAMILY.
    """
    db = QFontDatabase()
    family = ""
    for path in [
        str(_LC_DIR / "LibreClarendonNormal-162Bold.otf"),
        str(_LC_DIR / "LibreClarendonNormal-68Regular.otf"),
        str(_LC_DIR / "LibreClarendonNormal-110Medium.otf"),
        str(_LC_DIR / "LibreClarendonNormal-42Light.otf"),
        str(_FONTS_DIR / "Hanken_Grotesk" / "HankenGrotesk-VariableFont_wght.ttf"),
    ]:
        if Path(path).exists():
            font_id = db.addApplicationFont(path)
            if font_id != -1:
                families = db.applicationFontFamilies(font_id)
                if families and not family:
                    family = families[0]
    return family  # Qt will use the default system font if empty


# ---------------------------------------------------------------------------
# Page thumbnail widget
# ---------------------------------------------------------------------------

class _PageCell(QWidget):
    """A single flipbook page thumbnail.

    Fills the cell with the page background color, then draws the motif word
    (or cover text) centered in the foreground color using Libre Clarendon.
    """

    def __init__(
        self,
        page: dict,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._page = page

        # Tooltip shows shot index and time
        kind  = page.get("kind", "shot")
        idx   = page.get("shot_index")
        start = page.get("start_time", "")
        text  = page.get("text", "")

        if kind == "cover_front":
            film_motif = page.get("film_motif") or {}
            motif_val  = film_motif.get("value", "").strip()
            if motif_val and motif_val != text:
                tip = f"Front cover\n{text}\n(film motif: {motif_val})"
            else:
                tip = f"Front cover\n{text}"
        elif kind == "cover_back":
            back_title = page.get("back_title", text)
            back_year  = page.get("back_year", "")
            display    = f"{back_title}, {back_year}" if back_year else back_title
            tip = f"Back cover\n{display}"
        else:
            parts = []
            if idx is not None:
                parts.append(f"Shot {idx}")
            if start:
                parts.append(start)
            if text:
                parts.append(f"\"{text}\"")
            tip = "\n".join(parts)
        self.setToolTip(tip)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(int(120 * _ASPECT), 120)

    def paintEvent(self, _event) -> None:  # noqa: N802
        page = self._page
        bg_raw = page.get("bg_rgb") or [60, 60, 60]
        fg_raw = page.get("fg_rgb") or [180, 180, 180]
        text   = str(page.get("text") or "")

        bg_color = QColor(int(bg_raw[0]), int(bg_raw[1]), int(bg_raw[2]))
        fg_color = QColor(int(fg_raw[0]), int(fg_raw[1]), int(fg_raw[2]))

        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.fillRect(self.rect(), bg_color)

        kind   = page.get("kind", "shot")
        cell_w = self.width()
        cell_h = self.height()

        if text:
            # Start with a pixel size of 40% of cell height, then scale
            # down proportionally if the word is wider than 80% of cell width.
            target_px = max(6, int(cell_h * 0.40))
            font = QFont(_FONT_FAMILY or "")
            font.setPixelSize(target_px)

            avail_w = int(cell_w * 0.80)
            fm = QFontMetrics(font)
            tw = fm.horizontalAdvance(text)

            if tw > 0 and tw > avail_w:
                scaled_px = max(6, int(target_px * avail_w / tw))
                font.setPixelSize(scaled_px)
                fm = QFontMetrics(font)
                tw = fm.horizontalAdvance(text)

            th = fm.height()
            x = (cell_w - tw) // 2
            y = (cell_h - th) // 2 + fm.ascent()

            painter.setFont(font)
            painter.setPen(fg_color)
            painter.drawText(x, y, text)

        painter.end()


# ---------------------------------------------------------------------------
# Grid container (manual flow layout)
# ---------------------------------------------------------------------------

class _GridWidget(QWidget):
    """Holds page cells in a wrap-around grid that reflows on resize."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._cells: list[_PageCell] = []

    def load_pages(self, pages: list[dict]) -> None:
        for cell in self._cells:
            cell.setParent(None)   # type: ignore[arg-type]
            cell.deleteLater()
        self._cells = []

        for page in pages:
            cell = _PageCell(page, self)
            cell.show()
            self._cells.append(cell)

        self._reflow()

    def _reflow(self) -> None:
        n = len(self._cells)
        if n == 0:
            return

        W = max(1, self.width()  - 2 * _MARGIN)
        H = max(1, self.height() - 2 * _MARGIN)

        # Find the column count that maximises cell area at fixed aspect ratio.
        best_area = 0.0
        best_cols = 1
        best_cw   = 0.0
        best_ch   = 0.0

        for cols in range(1, n + 1):
            rows = math.ceil(n / cols)
            cw = (W - (cols - 1) * _GAP) / cols
            ch = cw / _ASPECT
            if rows * ch + (rows - 1) * _GAP > H:
                ch = (H - (rows - 1) * _GAP) / rows
                cw = ch * _ASPECT
            if cw <= 0 or ch <= 0:
                continue
            area = cw * ch
            if area > best_area:
                best_area = area
                best_cols = cols
                best_cw   = cw
                best_ch   = ch

        if best_area <= 0:
            return

        cell_w = max(1, int(best_cw))
        cell_h = max(1, int(best_ch))
        rows   = math.ceil(n / best_cols)

        grid_w = best_cols * cell_w + (best_cols - 1) * _GAP
        grid_h = rows      * cell_h + (rows      - 1) * _GAP
        x0 = _MARGIN + max(0, (W - grid_w) // 2)
        y0 = _MARGIN + max(0, (H - grid_h) // 2)

        for i, cell in enumerate(self._cells):
            row, col = divmod(i, best_cols)
            x = x0 + col * (cell_w + _GAP)
            y = y0 + row * (cell_h + _GAP)
            cell.setGeometry(x, y, cell_w, cell_h)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class FlipbookVisualizerWindow(QMainWindow):
    """Main window for the Flipbook Visualizer."""

    def __init__(
        self,
        project_path: str,
        media_type: str = "movie",
    ) -> None:
        super().__init__()
        self.setWindowTitle("Crossing — Flipbook Visualizer")
        self._project_path = project_path
        self._media_type   = media_type

        # List of (display_label, flipbook_data) tuples
        self._books: list[tuple[str, dict]] = []
        self._current_idx: int = 0
        self._updating_combo: bool = False

        self._build_ui()
        self._load_all_movies()
        restore_window_geometry(self, "window_flipbook")

    def closeEvent(self, event) -> None:  # noqa: N802
        save_window_geometry(self, "window_flipbook")
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(0, 6, 0, 0)
        vbox.setSpacing(4)

        # Top bar
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 0, 10, 0)
        bar.setSpacing(6)

        lbl = QLabel("Movie:")
        lbl.setStyleSheet(
            f"color: {theme.TEXT}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
        bar.addWidget(lbl)

        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        self._combo.installEventFilter(self)
        bar.addWidget(self._combo, 1)

        title_lbl = QLabel("Title:")
        title_lbl.setStyleSheet(
            f"color: {theme.TEXT}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
        bar.addWidget(title_lbl)

        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText("Film title motif — press Enter to save")
        self._title_edit.setStyleSheet(
            f"color: {theme.TEXT}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
            f" background: {theme.CANVAS_BG}; border: 1px solid {theme.TEXT_DIM};"
            f" padding: 2px 6px;"
        )
        self._title_edit.returnPressed.connect(self._on_title_edited)
        self._title_edit.installEventFilter(self)
        bar.addWidget(self._title_edit, 2)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
        bar.addWidget(self._status_label)

        self._export_btn = QPushButton("Export PDF")
        self._export_btn.setStyleSheet(
            f"color: {theme.TEXT}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
        self._export_btn.setToolTip("Render the current movie's flipbook as a PDF")
        self._export_btn.clicked.connect(self._on_export_pdf)
        bar.addWidget(self._export_btn)

        vbox.addLayout(bar)

        self._grid = _GridWidget()
        self._grid.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self._grid.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        vbox.addWidget(self._grid, 1)

        self.setMinimumSize(640, 400)
        self.resize(1200, 700)

    # ------------------------------------------------------------------
    # Data loading

    def _load_all_movies(self) -> None:
        from data.metadata import get_metadata
        from data.annotate import get_annotation_json_path

        meta_entries = get_metadata(self._project_path, media_type=self._media_type)

        books: list[tuple[str, dict]] = []
        for meta in meta_entries:
            filename = meta.get("filename")
            if not filename:
                continue
            json_path = get_annotation_json_path(
                self._project_path, filename, self._media_type,
            )
            if not json_path.exists():
                continue

            title = meta.get("title") or Path(filename).stem
            year  = meta.get("year")
            label = f"{title} ({year})" if year else title
            books.append((label, {"filename": filename, "loaded": False}))

        self._books = books
        self._updating_combo = True
        self._combo.clear()
        for label, _ in books:
            self._combo.addItem(label)
        self._updating_combo = False

        if books:
            self._show_movie(0)
        else:
            self._status_label.setText(
                "No annotated movies found. Run: crossing annotate shot --all"
            )

    def _ensure_loaded(self, idx: int) -> bool:
        """Lazy-load flipbook data for books[idx] if not yet done."""
        if idx < 0 or idx >= len(self._books):
            return False
        label, data = self._books[idx]
        if data.get("loaded"):
            return True

        filename = data.get("filename", "")
        try:
            from generators.flipbook import load_flipbook_data
            fb = load_flipbook_data(
                self._project_path, filename, self._media_type,
            )
            data.update(fb)
            data["loaded"] = True
            self._books[idx] = (label, data)
            return True
        except FileNotFoundError as exc:
            self._status_label.setText(str(exc))
            return False
        except Exception as exc:
            self._status_label.setText(f"Error: {exc}")
            return False

    # ------------------------------------------------------------------
    # Display

    def _show_movie(self, idx: int) -> None:
        if not self._books or idx < 0 or idx >= len(self._books):
            return

        self._current_idx = idx
        if not self._ensure_loaded(idx):
            return

        label, data = self._books[idx]
        pages     = data.get("pages", [])
        n_shots   = sum(1 for p in pages if p.get("kind") == "shot")
        n_total   = len(pages)
        has_motif = sum(
            1 for p in pages
            if p.get("kind") == "shot" and p.get("motif", "") not in ("—", "", None)
        )

        self._status_label.setText(
            f"{n_shots} shots  ·  {has_motif} with motif  ·  {n_total} pages total"
        )
        self._grid.load_pages(pages)

        # Populate the title edit with the current film title motif value
        film_motif = data.get("film_motif") or {}
        current_title = film_motif.get("value", "").strip() or data.get("title", "")
        self._title_edit.blockSignals(True)
        self._title_edit.setText(current_title)
        self._title_edit.setPlaceholderText(data.get("title", "") or "Film title motif")
        self._title_edit.blockSignals(False)

        self._updating_combo = True
        self._combo.setCurrentIndex(idx)
        self._updating_combo = False

    # ------------------------------------------------------------------
    # Title edit

    def _on_title_edited(self) -> None:
        """Save the edited film title motif and reload the current movie."""
        value = self._title_edit.text().strip()
        if not value or not self._books:
            return
        idx = self._current_idx
        label, data = self._books[idx]
        filename = data.get("filename", "")
        if not filename:
            return
        try:
            from data.film_motif import set_film_title
            set_film_title(self._project_path, filename, self._media_type, value)
            # Force reload so the front cover reflects the new value
            data["loaded"] = False
            self._books[idx] = (label, data)
            self._show_movie(idx)
        except Exception as exc:
            self._status_label.setText(f"Error saving title: {exc}")
        finally:
            self.setFocus()

    # ------------------------------------------------------------------
    # PDF export

    def _on_export_pdf(self) -> None:
        if not self._books:
            return
        idx = self._current_idx
        if not self._ensure_loaded(idx):
            return

        label, data = self._books[idx]
        filename = data.get("filename", "")
        title    = data.get("title", label)
        pages    = data.get("pages", [])

        from generators.flipbook import get_flipbook_output_path, export_flipbook_pdf

        output_path = get_flipbook_output_path(self._project_path, filename)

        self._export_btn.setEnabled(False)
        self._status_label.setText("Exporting PDF…")
        QApplication.processEvents()

        try:
            export_flipbook_pdf(pages, output_path, title=title, verbose=False)
            self._status_label.setText(f"Saved: {output_path}")

            # Try to open the PDF in the desktop viewer
            try:
                import subprocess
                subprocess.Popen(["xdg-open", str(output_path)])
            except Exception:
                pass

        except Exception as exc:
            QMessageBox.critical(
                self, "Export failed",
                f"Could not save PDF:\n{exc}",
            )
            self._status_label.setText("Export failed.")
        finally:
            self._export_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Signal handlers

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Intercept Home/End/PgUp/PgDn on the movie combo, and
        Tab/Backtab on the title edit."""
        if event.type() == QEvent.KeyPress:
            if obj is self._combo:
                key = event.key()
                if key == Qt.Key_Home:
                    if self._current_idx > 0:
                        self._show_movie(self._current_idx - 1)
                    return True
                if key == Qt.Key_End:
                    if self._current_idx < len(self._books) - 1:
                        self._show_movie(self._current_idx + 1)
                    return True
                if key in (Qt.Key_PageUp, Qt.Key_PageDown):
                    return True  # PgUp/PgDn are not used in this visualizer
            if obj is self._title_edit:
                if event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
                    self._on_title_edited()
                    return True
                if event.key() in (Qt.Key_Q, Qt.Key_W) and event.modifiers() & Qt.ControlModifier:
                    self.close()
                    return True
        return super().eventFilter(obj, event)

    def _on_combo_changed(self, idx: int) -> None:
        if self._updating_combo:
            return
        self._show_movie(idx)

    # ------------------------------------------------------------------
    # Keyboard navigation

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        mod = event.modifiers()

        if key == Qt.Key_Escape or (
            key in (Qt.Key_Q, Qt.Key_W) and mod & Qt.ControlModifier
        ):
            self.close()
            return

        if key == Qt.Key_P and mod & Qt.ControlModifier:
            self._on_export_pdf()
            return

        if key == Qt.Key_Tab:
            self._title_edit.setFocus()
            self._title_edit.selectAll()
            return

        if key == KEY_PREV_TITLE:
            if self._current_idx > 0:
                self._show_movie(self._current_idx - 1)
        elif key == KEY_NEXT_TITLE:
            if self._current_idx < len(self._books) - 1:
                self._show_movie(self._current_idx + 1)
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(
    project_path: str,
    media_type: str = "movie",
) -> None:
    """Create QApplication (if needed) and open the flipbook visualizer."""
    global _FONT_FAMILY
    from visualizers._window_helpers import raise_existing_window
    if raise_existing_window("flipbook"):
        return

    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    _FONT_FAMILY = _load_flipbook_font()
    win = FlipbookVisualizerWindow(project_path, media_type=media_type)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--media", default="movie")
    parsed = ap.parse_args()
    run_visualizer(parsed.project, media_type=parsed.media)
