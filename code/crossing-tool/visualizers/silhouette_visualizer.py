#!/usr/bin/env python3
"""Silhouette Visualizer — browse cached silhouette polygons for vocabulary words.

Launched via:
    crossing visualizer silhouette
    crossing visualizer silhouette --field animals
    crossing visualizer silhouette --media gameplay

Layout:
  LEFT   — full-bleed frame image with polygon overlay
  RIGHT  — field filter, scrollable record list, status block

Every cached JSON file is shown as its own row in the record list —
multiple silhouettes for the same word appear as separate entries.

Keyboard:
  Down / Up      — next / previous record in the list
  Right / Left   — jump to first record of next / previous word
  Escape / Ctrl+Q / Ctrl+W — close
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import save_window_geometry, restore_window_geometry

# Fix Qt plugin conflict with OpenCV — import PyQt5 before cv2
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QImage, QPixmap, QPainter, QPen, QPolygon, QColor, QFont
from PyQt5.QtCore import QPoint

import cv2
import numpy as np

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Frame + polygon rendering (unchanged from v1)
# ---------------------------------------------------------------------------

def _load_frame(
    project_path: str,
    payload: dict,
    media_type: str,
) -> Optional[np.ndarray]:
    """Return a BGR numpy array for the frame referenced by *payload*."""
    provenance: str = payload.get("frame_provenance", "")
    if provenance and not provenance.startswith("frame:"):
        p = Path(provenance)
        if not p.is_absolute():
            p = Path(project_path) / p
        if p.exists():
            img = cv2.imread(str(p))
            if img is not None:
                return img

    filename: str = payload.get("source_filename", "")
    frame_index: int = int(payload.get("frame_index", 0))
    if filename:
        video_path = Path(project_path) / "media" / "videos" / media_type / filename
        if video_path.exists():
            cap = cv2.VideoCapture(str(video_path))
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                ret, frame = cap.read()
                cap.release()
                if ret:
                    return frame

    return None


def _render_frame_with_polygon(
    bgr: np.ndarray,
    polygon: list,
    display_w: int,
    display_h: int,
) -> QPixmap:
    """Scale *bgr* to fit inside (display_w × display_h) and draw the polygon."""
    h_src, w_src = bgr.shape[:2]
    scale = min(display_w / w_src, display_h / h_src)
    scaled_w = int(w_src * scale)
    scaled_h = int(h_src * scale)

    scaled = cv2.resize(bgr, (scaled_w, scaled_h), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(scaled, cv2.COLOR_BGR2RGB)

    h, w, ch = rgb.shape
    qimg = QImage(rgb.data.tobytes(), w, h, ch * w, QImage.Format_RGB888)
    base_pixmap = QPixmap.fromImage(qimg)

    if not polygon:
        return base_pixmap

    result = QPixmap(base_pixmap.size())
    result.fill(Qt.black)
    painter = QPainter(result)
    painter.drawPixmap(0, 0, base_pixmap)

    pen = QPen(QColor(0, 255, 80))
    pen.setWidth(2)
    painter.setPen(pen)

    pts = QPolygon([
        QPoint(int(x * scale), int(y * scale))
        for x, y in polygon
    ])
    painter.drawPolygon(pts)
    painter.end()

    return result


def _make_empty_pixmap(w: int, h: int, message: str) -> QPixmap:
    pm = QPixmap(max(w, 1), max(h, 1))
    pm.fill(QColor(30, 30, 30))
    painter = QPainter(pm)
    painter.setPen(QColor(160, 160, 160))
    painter.drawText(pm.rect(), Qt.AlignCenter, message)
    painter.end()
    return pm


# ---------------------------------------------------------------------------
# Record list helpers
# ---------------------------------------------------------------------------

def _short_scope(scope: str) -> str:
    """Return a compact scope label for list display."""
    if scope == "all":
        return "all"
    if scope.startswith("movie-"):
        return scope[6:]   # e.g. "tmdb_11969"
    if scope.startswith("shot-"):
        return "shot"
    return scope


def _list_label(rec: dict) -> str:
    """One-line label for a record shown in the QListWidget."""
    scope = _short_scope(rec["scope"])
    score = rec["score"]
    return f"{rec['word']}  ·  {scope}  ·  {score:.3f}"


# ---------------------------------------------------------------------------
# Panel width
# ---------------------------------------------------------------------------

_PANEL_W = 290


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SilhouetteVisualizer(QMainWindow):
    """Read-only browser for cached silhouette polygons.

    Displays every cached JSON record individually — multiple silhouettes
    for the same word appear as separate entries in the record list.
    """

    def __init__(
        self,
        project_path: str,
        media_type: str = "movies",
        initial_field: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Crossing — Silhouette Visualizer")

        self._project_path = project_path
        self._media_type = media_type
        self._initial_field = initial_field

        # Flat list of record dicts (one per JSON file)
        self._records: list[dict] = []
        # Index into _records for the currently displayed entry
        self._record_idx: int = 0
        # Whether combo/list signals are being blocked during programmatic update
        self._updating: bool = False

        self._build_ui()
        self._reload_cache()
        restore_window_geometry(self, "window_silhouette")

    def closeEvent(self, event) -> None:
        save_window_geometry(self, "window_silhouette")
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        # Left: image canvas
        self._canvas = QLabel()
        self._canvas.setAlignment(Qt.AlignCenter)
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._canvas.setMinimumSize(400, 300)
        self._canvas.setStyleSheet("background: #1e1e1e;")
        row.addWidget(self._canvas, 1)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFrameShadow(QFrame.Sunken)
        row.addWidget(sep)

        # Right: control panel
        panel = QWidget()
        panel.setFixedWidth(_PANEL_W)
        panel.setStyleSheet(
            f"QWidget {{ background: {theme.PANEL_BG}; }}"
            f" QComboBox {{ background-color: {theme.INPUT_BG}; }}"
            f" QListWidget {{ background-color: {theme.INPUT_BG}; }}"
            f" QLabel {{ color: {theme.TEXT}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt; }}"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        # ── Scope group ───────────────────────────────────────────────
        scope_group = QGroupBox("Scope")
        scope_layout = QVBoxLayout(scope_group)
        scope_layout.setContentsMargins(8, 12, 8, 8)
        scope_layout.setSpacing(6)

        media_lbl = QLabel(f"Media: {self._media_type}")
        media_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        scope_layout.addWidget(media_lbl)

        field_hdr = QLabel("Field")
        field_hdr.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        scope_layout.addWidget(field_hdr)

        self._field_combo = QComboBox()
        self._field_combo.currentIndexChanged.connect(self._on_field_changed)
        scope_layout.addWidget(self._field_combo)
        layout.addWidget(scope_group)

        # ── Records group ─────────────────────────────────────────────
        records_group = QGroupBox("Records")
        records_layout = QVBoxLayout(records_group)
        records_layout.setContentsMargins(8, 12, 8, 8)
        records_layout.setSpacing(6)

        self._list_hdr = QLabel("0 records")
        self._list_hdr.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        records_layout.addWidget(self._list_hdr)

        self._record_list = QListWidget()
        self._record_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._record_list.currentRowChanged.connect(self._on_list_row_changed)
        records_layout.addWidget(self._record_list, 1)
        layout.addWidget(records_group, 1)

        # ── Info group ────────────────────────────────────────────────
        info_group = QGroupBox("Info")
        info_layout = QVBoxLayout(info_group)
        info_layout.setContentsMargins(8, 12, 8, 8)
        self._status_widget = _StatusBlock()
        info_layout.addWidget(self._status_widget)
        layout.addWidget(info_group)

        row.addWidget(panel)

        self.setMinimumSize(720, 420)
        self.resize(1100, 640)

    # ------------------------------------------------------------------
    # Cache loading

    def _reload_cache(self) -> None:
        from services.silhouette import scan_records_flat
        self._records = scan_records_flat(
            self._project_path,
            self._media_type,
            filter_field=self._initial_field,
        )
        self._rebuild_field_combo()
        self._rebuild_record_list()
        self._record_idx = 0
        self._show_current()

    # ------------------------------------------------------------------
    # Field combo

    def _rebuild_field_combo(self) -> None:
        self._updating = True
        self._field_combo.blockSignals(True)
        self._field_combo.clear()
        self._field_combo.addItem("— all fields —", None)

        # Collect unique fields from all records
        seen: set[str] = set()
        for rec in self._records:
            seen.add(rec["field"])
        for f in sorted(seen):
            self._field_combo.addItem(f, f)

        if self._initial_field:
            idx = self._field_combo.findData(self._initial_field)
            if idx >= 0:
                self._field_combo.setCurrentIndex(idx)

        self._field_combo.blockSignals(False)
        self._updating = False

    def _active_field(self) -> Optional[str]:
        return self._field_combo.currentData()

    def _filtered_records(self) -> list[dict]:
        """Return records matching the currently active field filter."""
        field = self._active_field()
        if field is None:
            return self._records
        return [r for r in self._records if r["field"] == field]

    # ------------------------------------------------------------------
    # Record list (QListWidget)

    def _rebuild_record_list(self) -> None:
        self._updating = True
        self._record_list.blockSignals(True)
        self._record_list.clear()

        visible = self._filtered_records()
        for rec in visible:
            item = QListWidgetItem(_list_label(rec))
            # Store the absolute index into self._records as item data
            item.setData(Qt.UserRole, self._records.index(rec))
            self._record_list.addItem(item)

        self._list_hdr.setText(f"{len(visible)} records")
        self._record_list.blockSignals(False)
        self._updating = False

    def _sync_list_to_idx(self) -> None:
        """Highlight the list row that matches self._record_idx."""
        self._record_list.blockSignals(True)
        visible = self._filtered_records()
        for row, rec in enumerate(visible):
            if self._records.index(rec) == self._record_idx:
                self._record_list.setCurrentRow(row)
                self._record_list.scrollToItem(self._record_list.currentItem())
                break
        self._record_list.blockSignals(False)

    # ------------------------------------------------------------------
    # Display

    def _show_current(self) -> None:
        visible = self._filtered_records()
        if not visible:
            self._canvas.setPixmap(
                _make_empty_pixmap(
                    self._canvas.width() or 600,
                    self._canvas.height() or 400,
                    "No silhouettes cached yet.\n"
                    "Run:  crossing index silhouette <word> --field <field>",
                )
            )
            self._status_widget.clear()
            self._sync_list_to_idx()
            return

        # Clamp to visible range
        if self._record_idx < 0:
            self._record_idx = 0
        # If current record is not in visible, jump to first visible
        visible_indices = [self._records.index(r) for r in visible]
        if self._record_idx not in visible_indices:
            self._record_idx = visible_indices[0]

        rec = self._records[self._record_idx]
        json_path: Path = rec["path"]

        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._canvas.setPixmap(
                _make_empty_pixmap(
                    self._canvas.width() or 600,
                    self._canvas.height() or 400,
                    f"Could not load:\n{json_path.name}\n{exc}",
                )
            )
            self._status_widget.clear()
            self._sync_list_to_idx()
            return

        bgr = _load_frame(self._project_path, payload, self._media_type)
        polygon = payload.get("polygon") or []
        cw = max(self._canvas.width(), 400)
        ch = max(self._canvas.height(), 300)

        if bgr is not None:
            pm = _render_frame_with_polygon(bgr, polygon, cw, ch)
        else:
            frame_size = payload.get("frame_size", [1920, 1080])
            fake = np.zeros((frame_size[1], frame_size[0], 3), dtype=np.uint8)
            pm = _render_frame_with_polygon(fake, polygon, cw, ch)
            if bgr is None and not polygon:
                pm = _make_empty_pixmap(cw, ch, "Frame image not found")

        self._canvas.setPixmap(pm)
        self._status_widget.update_from(payload, rec)
        self._sync_list_to_idx()

    # ------------------------------------------------------------------
    # Signal handlers

    def _on_field_changed(self, _idx: int) -> None:
        if self._updating:
            return
        self._rebuild_record_list()
        visible = self._filtered_records()
        if visible:
            self._record_idx = self._records.index(visible[0])
        self._show_current()

    def _on_list_row_changed(self, row: int) -> None:
        if self._updating or row < 0:
            return
        item = self._record_list.item(row)
        if item is None:
            return
        abs_idx = item.data(Qt.UserRole)
        if abs_idx is not None and abs_idx != self._record_idx:
            self._record_idx = abs_idx
            self._show_current()

    # ------------------------------------------------------------------
    # Keyboard navigation

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()

        if key in (Qt.Key_Q, Qt.Key_W) and mod & Qt.ControlModifier:
            self.close()
            return

        visible = self._filtered_records()
        if not visible:
            super().keyPressEvent(event)
            return

        visible_indices = [self._records.index(r) for r in visible]

        if key == Qt.Key_Down:
            # Next record in visible list
            try:
                pos = visible_indices.index(self._record_idx)
            except ValueError:
                pos = -1
            if pos < len(visible_indices) - 1:
                self._record_idx = visible_indices[pos + 1]
                self._show_current()

        elif key == Qt.Key_Up:
            # Previous record in visible list
            try:
                pos = visible_indices.index(self._record_idx)
            except ValueError:
                pos = len(visible_indices)
            if pos > 0:
                self._record_idx = visible_indices[pos - 1]
                self._show_current()

        elif key == Qt.Key_Right:
            # Jump to first record of the next word
            try:
                pos = visible_indices.index(self._record_idx)
            except ValueError:
                pos = -1
            cur_word = visible[pos]["word"] if 0 <= pos < len(visible) else None
            for i in range(pos + 1, len(visible)):
                if visible[i]["word"] != cur_word:
                    self._record_idx = visible_indices[i]
                    self._show_current()
                    break

        elif key == Qt.Key_Left:
            # Jump to first record of the previous word
            try:
                pos = visible_indices.index(self._record_idx)
            except ValueError:
                pos = len(visible)
            cur_word = visible[pos]["word"] if 0 <= pos < len(visible) else None
            # Walk back past all records with cur_word, then find the start of the one before
            i = pos - 1
            while i >= 0 and visible[i]["word"] == cur_word:
                i -= 1
            if i >= 0:
                prev_word = visible[i]["word"]
                while i > 0 and visible[i - 1]["word"] == prev_word:
                    i -= 1
                self._record_idx = visible_indices[i]
                self._show_current()

        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Resize

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._show_current()


# ---------------------------------------------------------------------------
# Status block widget
# ---------------------------------------------------------------------------

class _StatusBlock(QWidget):
    """Compact metadata panel for the currently displayed silhouette record."""

    _KEYS = [
        ("word",    "word"),
        ("field",   "field"),
        ("scope",   "scope"),
        ("score",   "score"),
        ("shot",    "shot_id"),
        ("file",    "source_filename"),
        ("polygon", None),   # special: computed
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep)

        self._labels: list[QLabel] = []
        for _ in self._KEYS:
            lbl = QLabel("")
            lbl.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-family: '{theme.FAMILY_UI}';"
                f" font-size: {theme.BASE_PT}pt;"
            )
            lbl.setWordWrap(True)
            layout.addWidget(lbl)
            self._labels.append(lbl)

    def clear(self) -> None:
        for lbl in self._labels:
            lbl.setText("")

    def update_from(self, payload: dict, rec: dict) -> None:
        values = {
            "word":             payload.get("word", rec.get("word", "—")),
            "field":            payload.get("field", rec.get("field", "—")),
            "scope":            payload.get("scope", rec.get("scope", "—")),
            "score":            f"{payload.get('score', rec.get('score', 0.0)):.4f}",
            "shot_id":          payload.get("shot_id", "—"),
            "source_filename":  payload.get("source_filename", "—"),
        }
        polygon_pts = len(payload.get("polygon") or [])

        for lbl, (display_key, payload_key) in zip(self._labels, self._KEYS):
            if payload_key is None:
                lbl.setText(f"polygon: {polygon_pts} pts")
            else:
                lbl.setText(f"{display_key}: {values[payload_key]}")


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(
    project_path: str,
    media_type: str = "movies",
    field: Optional[str] = None,
) -> None:
    """Create QApplication (if needed) and open the silhouette visualizer."""
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    win = SilhouetteVisualizer(project_path, media_type=media_type, initial_field=field)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--media", default="movies")
    ap.add_argument("--field", default=None)
    parsed = ap.parse_args()
    run_visualizer(parsed.project, media_type=parsed.media, field=parsed.field)

