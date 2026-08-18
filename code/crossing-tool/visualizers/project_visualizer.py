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
import tempfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.combo_popup import style_canonical_combo
from visualizers.components.inspector import Inspector
from visualizers.components.metadata_block import (
    INSPECTOR_ROW_HEIGHT,
    table_key_cell_style,
    table_ui_cell_style,
)
from visualizers.components.sweep_bar import SweepBar
from visualizers.components.tab_panel import TabPanel

from PyQt5.QtCore import Qt, QEvent, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QCursor, QFontMetrics
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLineEdit, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QTabBar, QToolTip, QVBoxLayout, QWidget, QLabel,
)

from tool import prefs as _prefs
from tool.shortcuts import shortcut_label_for

_CLI_PATH = Path(__file__).parent.parent / "cli.py"
_THUMBNAIL_PALETTE_MEDIA_TYPES = ("movie", "gameplay")


def _start_thumbnail_palette_cli(
    project_path: str,
    media_type: str,
    output_stream,
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(_CLI_PATH),
            "index", "palette", "create",
            "--thumbnail", "--all",
            "--media", media_type,
        ],
        cwd=project_path,
        stdout=output_stream,
        stderr=subprocess.STDOUT,
    )


def _start_vocabulary_cli(
    project_path: str,
    output_stream,
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(_CLI_PATH),
            "index", "vocabulary", "--all", "--force",
        ],
        cwd=project_path,
        stdout=output_stream,
        stderr=subprocess.STDOUT,
    )


def _start_illustration_cli(
    project_path: str,
    output_stream,
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(_CLI_PATH),
            "index", "illustration", "--media", "both",
        ],
        cwd=project_path,
        stdout=output_stream,
        stderr=subprocess.STDOUT,
    )


def _start_untyped_audit_cli(
    project_path: str,
    source: str,
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(_CLI_PATH),
            "index", "untyped", "--source", source,
        ],
        cwd=project_path,
    )


def _backup_free_bytes(backup_path: str) -> Optional[int]:
    """Return free bytes on the backup volume, or ``None`` if unreadable."""
    import shutil
    try:
        return shutil.disk_usage(str(backup_path)).free
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Constants mirrored from cli.py to avoid importing the full CLI module
# ---------------------------------------------------------------------------

# Mirrors cli.py's BACKUP_MIN_FREE_BYTES so the button warns exactly when
# `crossing backup update` would refuse to run.
_BACKUP_MIN_FREE_BYTES = 100 * 1024 * 1024

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

_COLUMN_ROW_H = 28   # shared fixed height for HEADER and COUNT regions (px)
_PROJECT_ROW_SEAM_H = 1
_PROJECT_COLUMN_SPACING = _PROJECT_ROW_SEAM_H
_SPACER_ROW_INSET = 8
_SPACER_ROW_GAP = 6


def _project_row_border_style() -> str:
    """Return the Project COUNT left, right, and bottom frame."""
    return (
        f"border-left: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER}; "
        f"border-right: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER}; "
        f"border-bottom: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER};"
    )


def _project_row_side_style() -> str:
    """Return Project left/right framing for rows separated by real gaps."""
    return (
        f"border-left: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER}; "
        f"border-right: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER};"
    )


def _project_datavis_horizontal_insets() -> tuple[int, int]:
    """Return insets that make adjacent DATAVIS content exactly one canonical gap apart."""
    inset_total = max(0, theme.INSPECTOR_GAP - _PROJECT_COLUMN_SPACING)
    left = inset_total // 2
    return left, inset_total - left

# Display text for a column's status line when it isn't "ready" — keyed by
# ProjectColumn.reason first, falling back to a generic per-state label.
# Never invented data: purely maps the explicit missing/stale reason
# services.corpus_stats already reports into short display text.
_COLUMN_REASON_LABELS = {
    "silhouette_index_missing": "NEEDS INDEX",
    "silhouette_index_stale": "INDEX STALE",
    "silhouette_index_error": "INDEX ERROR",
    "engraving_index_missing": "NEEDS INDEX",
    "engraving_index_stale": "INDEX STALE",
    "engraving_index_error": "INDEX ERROR",
    "vocabulary_index_stale": "INDEX STALE",
    "vocabulary_count_mismatch": "COUNT MISMATCH",
    "shot_type_stats_invalid": "STATS STALE",
    "corpus_stats_missing": "STATS REQUIRED",
    "corpus_stats_stale": "STATS STALE",
    "no_project": "NO PROJECT",
}
_COLUMN_STATE_FALLBACK_LABELS = {
    "unavailable": "UNAVAILABLE",
    "stale": "STALE",
}

_PROJECT_TIER_COLUMN_IDS = {
    "live": ("movies", "gameplay", "silhouettes", "engravings"),
    "cached": ("shots", "vocabulary"),
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


def _proportional_heights(
    counts: list[int], available_height: int, minimum_height: int = 0,
) -> list[int]:
    """Allocate exactly *available_height*, enforcing readable minima when possible."""
    if available_height <= 0 or not counts:
        return [0] * len(counts)
    positive_counts = [max(0, count) for count in counts]
    total = sum(positive_counts)
    if total <= 0:
        return [0] * len(counts)

    def allocate(indices: list[int], height: int) -> dict[int, int]:
        subtotal = sum(positive_counts[index] for index in indices)
        allocated = {
            index: height * positive_counts[index] // subtotal
            for index in indices
        }
        allocated[indices[-1]] += height - sum(allocated.values())
        return allocated

    # If readable minima cannot all fit, retain contained proportional
    # slices. Labels are suppressed only on slices below one readable line.
    if minimum_height <= 0 or len(counts) * minimum_height > available_height:
        allocated = allocate(list(range(len(counts))), available_height)
        return [allocated[index] for index in range(len(counts))]

    heights = [0] * len(counts)
    flexible = list(range(len(counts)))
    remaining_height = available_height
    while flexible:
        subtotal = sum(positive_counts[index] for index in flexible)
        undersized = [
            index for index in flexible
            if remaining_height * positive_counts[index] < minimum_height * subtotal
        ]
        if not undersized:
            allocated = allocate(flexible, remaining_height)
            for index, height in allocated.items():
                heights[index] = height
            break
        for index in undersized:
            heights[index] = minimum_height
            remaining_height -= minimum_height
        flexible = [index for index in flexible if index not in undersized]
    return heights


class _WordCountCell(QFrame):
    """Stretched word/count row using Project launcher spacing and row seams."""

    def __init__(self, font, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("VocabularyFieldCell")
        self._frame_style = (
            f"QFrame#VocabularyFieldCell {{ background: {theme.CELL_BG}; "
            f"{_project_row_side_style()} }}"
        )
        self._warning_frame_style = (
            f"QFrame#VocabularyFieldCell {{ background: {theme.WARNING_COLOR}; "
            f"{_project_row_side_style()} }}"
        )
        self.setStyleSheet(self._frame_style)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(_SPACER_ROW_INSET, 0, _PROJECT_ROW_SEAM_H, 0)
        layout.setSpacing(_SPACER_ROW_GAP)

        self.field_label = QLabel()
        self.field_label.setFont(font)
        self.field_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._field_style = table_ui_cell_style(
            "", "", include_minimum_height=False,
        )
        self._warning_field_style = table_ui_cell_style(
            "", "", background_color=theme.WARNING_COLOR,
            include_minimum_height=False,
        )
        self.field_label.setStyleSheet(self._field_style)
        layout.addWidget(self.field_label, 1)

        self.count_label = QLabel()
        self.count_label.setFont(font)
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._count_style = table_ui_cell_style(
            "",
            "",
            background_color=theme.CELL_BG,
            text_color=theme.TEXT_DIM,
            include_minimum_height=False,
            horizontal_padding=_SPACER_ROW_INSET,
        )
        self._compact_count_style = (
            self._count_style
            + f" padding-left: {_SPACER_ROW_GAP}px;"
            + f" padding-right: {_SPACER_ROW_INSET}px;"
        )
        self._warning_count_style = table_ui_cell_style(
            "",
            "",
            background_color=theme.WARNING_COLOR,
            include_minimum_height=False,
            horizontal_padding=_SPACER_ROW_INSET,
        )
        self._compact_warning_count_style = (
            self._warning_count_style
            + f" padding-left: {_SPACER_ROW_GAP}px;"
            + f" padding-right: {_SPACER_ROW_INSET}px;"
        )
        self.count_label.setStyleSheet(self._count_style)
        layout.addWidget(self.count_label)

    def prepare_count(self, count: str) -> None:
        self.count_label.setMinimumWidth(0)
        self.count_label.setMaximumWidth(16777215)
        self.count_label.setStyleSheet(self._count_style)
        self.count_label.setText(count)

    def set_content(
        self,
        field: str,
        count: str,
        count_width: int,
        visible: bool,
        compact_count: bool = False,
        warning: bool = False,
    ) -> None:
        self.setStyleSheet(
            self._warning_frame_style if warning else self._frame_style
        )
        self.field_label.setStyleSheet(
            self._warning_field_style if warning else self._field_style
        )
        if warning:
            count_style = (
                self._compact_warning_count_style
                if compact_count else self._warning_count_style
            )
        else:
            count_style = (
                self._compact_count_style if compact_count else self._count_style
            )
        self.count_label.setStyleSheet(count_style)
        self.field_label.setText(field if visible else "")
        self.count_label.setText(count if visible else "")
        self.count_label.setFixedWidth(count_width)


_VocabularyFieldCell = _WordCountCell


def _word_count_fields(datavis: dict) -> list[dict]:
    """Normalize renderer-neutral word/count payloads for shared presentation."""
    name_key = {
        "vocabulary_fields": "field",
        "shot_types": "name",
        "silhouette_fields": "field",
        "engraving_fields": "field",
    }.get(datavis.get("kind"))
    fields = datavis.get("fields", []) if name_key else []
    return [
        {
            "field": str(item.get(name_key, "")),
            "count": int(item.get("count", 0)),
            "synthetic": item.get("synthetic") is True,
        }
        for item in fields
        if item.get(name_key) and int(item.get("count", 0)) > 0
    ]


class _WordCountDatavisWidget(QWidget):
    """Responsive vertical composition shared by renderer-neutral word/count kinds."""

    syntheticActivated = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._cell_font = theme.font_ui()
        self._minimum_cell_height = INSPECTOR_ROW_HEIGHT
        self._fields: list[dict] = []
        self._field_cells: list[_VocabularyFieldCell] = []
        self._shared_count_width = True
        self._word_count_kind = "empty"

    def set_datavis(self, datavis: dict) -> None:
        self._word_count_kind = str(datavis.get("kind") or "empty")
        self._fields = _word_count_fields(datavis)
        self._shared_count_width = datavis.get("kind") != "shot_types"
        self.setStyleSheet(f"background: {theme.CANVAS_BG};")
        while len(self._field_cells) < len(self._fields):
            cell = _WordCountCell(self._cell_font, self)
            cell.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._field_cells.append(cell)
        for index, cell in enumerate(self._field_cells):
            cell.setVisible(index < len(self._fields))
        self._layout_cells()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            for item, cell in zip(self._fields, self._field_cells):
                if item["synthetic"] and cell.geometry().contains(event.pos()):
                    self.syntheticActivated.emit(self._word_count_kind)
                    event.accept()
                    return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_cells()

    def _layout_cells(self) -> None:
        gap_count = len(self._fields)
        total_gap_height = gap_count * theme.INSPECTOR_GAP
        if total_gap_height <= self.height():
            gap_heights = [theme.INSPECTOR_GAP] * gap_count
            available_cell_height = self.height() - total_gap_height
            heights = _proportional_heights(
                [item["count"] for item in self._fields],
                available_cell_height,
                self._minimum_cell_height,
            )
        else:
            # Degenerate density: canonical gaps alone do not fit. Cells
            # collapse to zero height and the existing leading/inter-field
            # CANVAS_BG gaps compress evenly to exactly the available height.
            # Later gaps receive the integer remainder; no gap exceeds the
            # canonical size and no geometry can escape the DATAVIS bounds.
            heights = [0] * gap_count
            base_gap, remainder = divmod(self.height(), gap_count)
            gap_heights = [
                base_gap + (index >= gap_count - remainder)
                for index in range(gap_count)
            ]
        y = gap_heights[0] if gap_heights else 0
        formatted_counts = [_format_column_count(item["count"]) for item in self._fields]
        for cell, count in zip(self._field_cells, formatted_counts):
            cell.prepare_count(count)
        count_width = max(
            (cell.count_label.sizeHint().width() for cell in self._field_cells[:len(self._fields)]),
            default=0,
        )
        for index, (item, height) in enumerate(zip(self._fields, heights)):
            cell = self._field_cells[index]
            count = formatted_counts[index]
            field_width = QFontMetrics(self._cell_font).horizontalAdvance(item["field"])
            shared_available_width = (
                self.width() - _SPACER_ROW_INSET - _SPACER_ROW_GAP - count_width
            )
            intrinsic_count_width = cell.count_label.sizeHint().width()
            compact_count_width = (
                intrinsic_count_width - _SPACER_ROW_INSET + _SPACER_ROW_GAP
            )
            intrinsic_available_width = (
                self.width() - _SPACER_ROW_INSET - _SPACER_ROW_GAP
                - compact_count_width
            )
            use_intrinsic_count_width = (
                not self._shared_count_width
                and field_width > shared_available_width
                and field_width <= intrinsic_available_width
            )
            row_count_width = (
                compact_count_width if use_intrinsic_count_width else count_width
            )
            available_field_width = (
                self.width() - _SPACER_ROW_INSET - _SPACER_ROW_GAP - row_count_width
            )
            text_fits = field_width <= available_field_width
            cell.set_content(
                item["field"],
                count,
                row_count_width,
                height >= self._minimum_cell_height and text_fits,
                compact_count=use_intrinsic_count_width,
                warning=item["synthetic"],
            )
            cell.setGeometry(0, y, self.width(), height)
            y += height
            if index + 1 < gap_count:
                y += gap_heights[index + 1]


_VocabularyDatavisWidget = _WordCountDatavisWidget


class _MediaItemCell(QWidget):
    """One anonymous media item; geometry and color are independent."""

    activated = pyqtSignal(dict)

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self.item: dict = {}
        self._default_color = color
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {color}; border: none;")
        self.setCursor(Qt.PointingHandCursor)

    def set_item(self, item: dict) -> None:
        self.item = item
        rgb = item.get("thumbnail_foreground_rgb")
        color = self._default_color
        if (
            isinstance(rgb, list)
            and len(rgb) == 3
            and all(
                isinstance(channel, int)
                and not isinstance(channel, bool)
                and 0 <= channel <= 255
                for channel in rgb
            )
        ):
            color = f"rgb({rgb[0]}, {rgb[1]}, {rgb[2]})"
        self.setStyleSheet(f"background: {color}; border: none;")
        self.setToolTip(str(item.get("title") or ""))

    def enterEvent(self, event) -> None:  # noqa: N802
        title = self.toolTip()
        if title:
            QToolTip.showText(QCursor.pos(), title, self)
        super().enterEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.item:
            QToolTip.hideText()
            self.activated.emit(self.item)
        super().mouseDoubleClickEvent(event)


class _ProjectDatavisWidget(_WordCountDatavisWidget):
    """Dispatch renderer-neutral Project DATAVIS payloads by ``kind``."""

    def __init__(self, parent=None) -> None:
        self._datavis_kind = "empty"
        self._media_items: list[dict] = []
        self._media_item_cells: list[_MediaItemCell] = []
        super().__init__(parent)
        self._content_widget = QWidget(self)
        self._content_widget.setObjectName("ProjectDatavisContent")
        self._content_widget.setAttribute(Qt.WA_StyledBackground, True)
        self._content_widget.setStyleSheet(
            f"QWidget#ProjectDatavisContent {{ background: {theme.CANVAS_BG}; }}"
        )
        self._content_widget.hide()

    mediaItemActivated = pyqtSignal(dict)

    def set_datavis(self, datavis: dict) -> None:
        self._datavis_kind = datavis.get("kind", "empty")
        if self._datavis_kind == "media_items":
            super().set_datavis({"kind": "empty"})
            self._content_widget.show()
            items = datavis.get("items", [])
            self._media_items = [item for item in items if isinstance(item, dict)]
            colors = (theme.CELL_BG, theme.PANEL_BG)
            while len(self._media_item_cells) < len(self._media_items):
                index = len(self._media_item_cells)
                cell = _MediaItemCell(
                    colors[index % len(colors)], self._content_widget
                )
                cell.activated.connect(self.mediaItemActivated)
                self._media_item_cells.append(cell)
            for index, cell in enumerate(self._media_item_cells):
                visible = index < len(self._media_items)
                cell.setVisible(visible)
                if visible:
                    cell.set_item(self._media_items[index])
            self._layout_media_items()
            return

        self._media_items = []
        self._content_widget.hide()
        for cell in self._media_item_cells:
            cell.hide()
        super().set_datavis(datavis)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_content_geometry()
        if self._datavis_kind == "media_items":
            self._layout_media_items()

    def _sync_content_geometry(self) -> None:
        left, right = _project_datavis_horizontal_insets()
        self._content_widget.setGeometry(
            left, 0, max(0, self.width() - left - right), self.height()
        )

    def _layout_media_items(self) -> None:
        item_count = len(self._media_items)
        if item_count <= 0:
            return
        self._sync_content_geometry()
        content = self._content_widget
        top_gap = min(theme.INSPECTOR_GAP, content.height())
        usable_height = max(0, content.height() - top_gap)
        base_height, remainder = divmod(usable_height, item_count)
        y = top_gap
        for index, cell in enumerate(self._media_item_cells[:item_count]):
            height = base_height + (index < remainder)
            cell.setGeometry(0, y, content.width(), height)
            y += height

    def leaveEvent(self, event) -> None:  # noqa: N802
        QToolTip.hideText()
        super().leaveEvent(event)


class _ProjectColumnWidget(QWidget):
    """One HEADER / COUNT / DATAVIS column of the Project Visualizer's V0 grid.

    A pure renderer over a single ``services.corpus_stats.ProjectColumn`` —
    it never computes or invents project data itself, only formats and lays
    out whatever column it is given via the constructor / ``set_column()``.
    ``column.state`` ("loading" / "ready" / "unavailable" / "stale") drives
    whether the shared SweepBar loading indicator (same one used by
    Illustration) is active and what the COUNT region shows. "unavailable"
    and "stale" are rendered distinctly from a real zero. Shots and Vocabulary
    may additionally carry ordered word/count data for their DATAVIS regions.
    """

    def __init__(self, column, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ProjectColumnWidget")
        self.setStyleSheet(
            f"QWidget#ProjectColumnWidget {{ background: {theme.CELL_BG}; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._header_label = QLabel()
        self._header_label.setAlignment(Qt.AlignCenter)
        self._header_label.setFont(theme.font_ui(bold=True))
        self._header_label.setStyleSheet(
            f"background: {theme.TITLE_BG}; color: {theme.TEXT_DIM}; "
            f"border: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER};"
        )
        self._header_label.setFixedHeight(_COLUMN_ROW_H)
        outer.addWidget(self._header_label)

        # Shared loading indicator (see visualizers/components/sweep_bar.py),
        # the same one Illustration uses — a thin ACCENT stripe, invisible
        # while idle. Driven by ProjectVisualizer's shared timer via tick().
        self._loading_bar = SweepBar(self)
        self._loading_gap = QWidget(self)
        self._loading_gap.setAttribute(Qt.WA_StyledBackground, True)
        self._loading_gap.setStyleSheet(
            f"background: {theme.CANVAS_BG}; border: none;"
        )
        self._loading_gap.setFixedHeight(theme.INSPECTOR_GAP)
        loading_layout = QVBoxLayout(self._loading_gap)
        loading_layout.setContentsMargins(0, 0, 0, 0)
        loading_layout.setSpacing(0)
        loading_layout.addWidget(self._loading_bar)
        outer.addWidget(self._loading_gap)

        self._count_label = QLabel()
        self._count_label.setAlignment(Qt.AlignCenter)
        self._count_label.setWordWrap(True)
        self._ready_font = theme.font_mono(bold=True)
        self._ready_font.setPointSize(theme.BASE_PT + 10)
        self._status_font = theme.font_ui(bold=True)
        self._status_font.setPointSize(theme.BASE_PT + 1)
        self._count_label.setFont(self._ready_font)
        self._count_border_style = _project_row_border_style()
        self._count_background_style = f"background: {theme.PANEL_BG};"
        self._count_label.setStyleSheet(
            f"{self._count_background_style} color: {theme.TEXT}; {self._count_border_style}"
        )
        self._count_label.setFixedHeight(_COLUMN_ROW_H)
        outer.addWidget(self._count_label)

        self._datavis_widget = _ProjectDatavisWidget()
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
            self._count_label.setStyleSheet(
                f"{self._count_background_style} color: {theme.TEXT}; {self._count_border_style}"
            )
            self._count_label.setText("loading…")
        elif column.state == "ready":
            self._loading_bar.stop()
            self._count_label.setFont(self._ready_font)
            self._count_label.setStyleSheet(
                f"{self._count_background_style} color: {theme.TEXT}; {self._count_border_style}"
            )
            self._count_label.setText(_format_column_count(column.count))
        elif column.state == "unavailable":
            self._loading_bar.stop()
            self._count_label.setFont(self._status_font)
            self._count_label.setStyleSheet(
                f"{self._count_background_style} color: {theme.TEXT_DIM}; {self._count_border_style}"
            )
            self._count_label.setText(
                "NEEDS INDEX"
                if column.reason == "illustration_index_missing"
                else "→ INDEX"
            )
        else:
            # "stale" remains distinct from both unavailable and a real "0".
            self._loading_bar.stop()
            self._count_label.setFont(self._status_font)
            status_label = _column_status_label(column)
            background = (
                theme.WARNING_COLOR
                if status_label == "INDEX STALE"
                else theme.PANEL_BG
            )
            text_color = (
                theme.TEXT
                if status_label == "INDEX STALE"
                else theme.TEXT_DIM
            )
            self._count_label.setStyleSheet(
                f"background: {background}; color: {text_color}; {self._count_border_style}"
            )
            self._count_label.setText(status_label)
        self._datavis_widget.set_datavis(
            column.datavis
            if column.state in ("ready", "stale")
            else {"kind": "empty"}
        )


class _ProjectColumnsWorker(QThread):
    """Load Project Visualizer columns off the GUI thread.

    Follows the same QThread + signal pattern as Mosaic's VocabularyWorker /
    Illustration's index workers: heavy work happens in ``run()``, results
    are delivered to the GUI thread via a signal. Never performs a full
    corpus traversal and never silently rebuilds a missing/stale artifact —
    see services.corpus_stats.get_live_project_columns /
    get_cached_project_columns.

    Emits ``tier_ready(generation, list)`` for each successful tier and
    ``tier_failed(generation, tier, message)`` for each failed tier. The cheap "live" tier
    (Movies/Gameplay/Silhouettes/Engravings — computed from metadata, palette
    caches, and the illustration index), and again for the
    persisted-cache tier (Shots/Vocabulary — reported as
    "unavailable"/"stale" rather than recomputed if the cache is missing or
    out of date) — so the GUI can display each tier as soon as it's ready
    instead of waiting for both.
    """

    tier_ready = pyqtSignal(int, list)  # generation, list[ProjectColumn]
    tier_failed = pyqtSignal(int, str, str)  # generation, tier, error message

    def __init__(self, project_path, generation: int = 0, parent=None) -> None:
        super().__init__(parent)
        self.project_path = project_path
        self.generation = generation

    def run(self) -> None:
        try:
            from services.corpus_stats import get_cached_project_columns, get_live_project_columns
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.tier_failed.emit(self.generation, "live", message)
            self.tier_failed.emit(self.generation, "cached", message)
            return

        for tier, getter in (
            ("live", get_live_project_columns),
            ("cached", get_cached_project_columns),
        ):
            try:
                columns = getter(self.project_path)
            except Exception as exc:
                self.tier_failed.emit(
                    self.generation, tier, f"{type(exc).__name__}: {exc}",
                )
            else:
                self.tier_ready.emit(self.generation, columns)


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
        layout.setContentsMargins(_SPACER_ROW_INSET, 0, _SPACER_ROW_INSET, 0)
        layout.setSpacing(_SPACER_ROW_GAP)

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
        self._thumbnail_palette_proc: subprocess.Popen | None = None
        self._thumbnail_palette_output = None
        self._thumbnail_palette_media_queue: list[str] = []
        self._thumbnail_palette_active_media = ""
        self._thumbnail_palette_project_path = ""
        self._thumbnail_palette_errors: list[str] = []
        self._vocabulary_proc: subprocess.Popen | None = None
        self._vocabulary_output = None
        self._vocabulary_project_path = ""
        self._illustration_proc: subprocess.Popen | None = None
        self._illustration_output = None
        self._index_all_proc: subprocess.Popen | None = None
        self._index_all_output = None
        self._index_all_project_path = ""
        self._index_all_queue: list[tuple[str, str, str | None]] = []
        self._index_all_errors: list[str] = []
        self._index_all_active_label = ""

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

        self.project_browse_btn = QPushButton("Browse…")
        self.project_browse_btn.clicked.connect(self._on_browse)
        self.project_browse_btn.setStyleSheet(theme.action_button_stylesheet())
        outer.addWidget(self.project_browse_btn)

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
        backup_path = _prefs.get("backup_path")
        if not backup_path:
            self.backup_btn.setEnabled(False)
            self._set_backup_space_warning(None)
            return
        p = Path(backup_path)
        if not p.exists() or not os.access(str(p), os.W_OK):
            self.backup_btn.setEnabled(False)
            self._set_backup_space_warning(None)
            return
        self.backup_btn.setEnabled(True)
        free = _backup_free_bytes(str(p))
        self._set_backup_space_warning(
            free if free is not None and free < _BACKUP_MIN_FREE_BYTES else None
        )

    def _set_backup_space_warning(self, free_bytes: Optional[int]) -> None:
        """Show the out-of-space state on the Backup button, or clear it."""
        if free_bytes is None:
            self.backup_btn.setText("Backup")
            self.backup_btn.setStyleSheet(theme.action_button_stylesheet())
            self.backup_btn.setToolTip("")
            return
        free_gb = free_bytes / (1024 ** 3)
        self.backup_btn.setText(f"Free Space: {free_gb:.0f} GB")
        self.backup_btn.setStyleSheet(theme.action_button_stylesheet(warning=True))
        self.backup_btn.setToolTip(
            f"The backup volume is full ({free_gb:.1f} GB free) — "
            "backups will not sync until space is freed."
        )

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
            self._refresh_backup_button()

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

    # Project tools

    def _build_tools_section(self) -> CollapsibleSection:
        sec = CollapsibleSection("Tools", pref_key="project_section_tools")

        self._tools_buttons_widget = QWidget()
        buttons_layout = QGridLayout(self._tools_buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(theme.INSPECTOR_GAP)
        buttons_layout.setColumnStretch(0, 1)
        buttons_layout.setColumnStretch(1, 1)

        self.thumbnail_palettes_btn = QPushButton("Index Thumbnails")
        self.thumbnail_palettes_btn.clicked.connect(self._on_thumbnail_palettes)
        self.thumbnail_palettes_btn.setStyleSheet(theme.action_button_stylesheet())
        self.thumbnail_palettes_btn.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed,
        )
        buttons_layout.addWidget(self.thumbnail_palettes_btn, 0, 0)

        self.rebuild_vocabulary_btn = QPushButton("Index Vocabulary")
        self.rebuild_vocabulary_btn.clicked.connect(self._on_rebuild_vocabulary)
        self.rebuild_vocabulary_btn.setStyleSheet(theme.action_button_stylesheet())
        self.rebuild_vocabulary_btn.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed,
        )
        buttons_layout.addWidget(self.rebuild_vocabulary_btn, 0, 1)

        self.index_illustrations_btn = QPushButton("Index Illustrations")
        self.index_illustrations_btn.clicked.connect(self._on_index_illustrations)
        self.index_illustrations_btn.setStyleSheet(theme.action_button_stylesheet())
        self.index_illustrations_btn.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed,
        )
        buttons_layout.addWidget(self.index_illustrations_btn, 1, 0)

        self.index_all_btn = QPushButton("Index")
        self.index_all_btn.clicked.connect(self._on_index_all)
        self.index_all_btn.setStyleSheet(theme.action_button_stylesheet())
        self.index_all_btn.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed,
        )
        buttons_layout.addWidget(self.index_all_btn, 1, 1)
        sec.add_widget(self._tools_buttons_widget)

        self._tools_loading_bar = SweepBar(self)
        self._tools_loading_timer = QTimer(self)
        self._tools_loading_timer.setInterval(20)
        self._tools_loading_timer.timeout.connect(self._tools_loading_bar.tick)
        self._thumbnail_palette_poll_timer = QTimer(self)
        self._thumbnail_palette_poll_timer.setInterval(250)
        self._thumbnail_palette_poll_timer.timeout.connect(
            self._poll_thumbnail_palette_cli
        )
        self._vocabulary_poll_timer = QTimer(self)
        self._vocabulary_poll_timer.setInterval(250)
        self._vocabulary_poll_timer.timeout.connect(self._poll_vocabulary_cli)
        self._illustration_poll_timer = QTimer(self)
        self._illustration_poll_timer.setInterval(250)
        self._illustration_poll_timer.timeout.connect(self._poll_illustration_cli)
        self._index_all_poll_timer = QTimer(self)
        self._index_all_poll_timer.setInterval(250)
        self._index_all_poll_timer.timeout.connect(self._poll_index_all_cli)
        sec.set_subbar(self._tools_loading_bar)
        self._tools_section = sec
        return sec

    def _tools_indexing(self) -> bool:
        return any((
            self._thumbnail_palette_proc is not None,
            self._vocabulary_proc is not None,
            self._illustration_proc is not None,
            self._index_all_proc is not None,
            bool(self._index_all_queue),
        ))

    def _begin_tools_indexing(self, active_button: QPushButton) -> None:
        for button in (
            self.thumbnail_palettes_btn,
            self.rebuild_vocabulary_btn,
            self.index_illustrations_btn,
            self.index_all_btn,
        ):
            button.setEnabled(False)
        active_button.setText("indexing")
        self.project_browse_btn.setEnabled(False)
        self._tools_loading_bar.start()
        self._tools_loading_timer.start()

    def _finish_tools_indexing(self) -> None:
        self._tools_loading_timer.stop()
        self._tools_loading_bar.stop()
        self.thumbnail_palettes_btn.setText("Index Thumbnails")
        self.rebuild_vocabulary_btn.setText("Index Vocabulary")
        self.index_illustrations_btn.setText("Index Illustrations")
        self.index_all_btn.setText("Index")
        self.thumbnail_palettes_btn.setEnabled(True)
        self.rebuild_vocabulary_btn.setEnabled(True)
        self.index_illustrations_btn.setEnabled(True)
        self.index_all_btn.setEnabled(True)
        self.project_browse_btn.setEnabled(True)

    def _on_thumbnail_palettes(self) -> None:
        if self._tools_indexing():
            return

        project_path = self.path_edit.text().strip()
        if not project_path:
            QMessageBox.warning(
                self, "No Project", "Please set a project folder first.",
            )
            return

        if _prefs.get("path") != project_path:
            _prefs.set("path", project_path)

        self._thumbnail_palette_project_path = project_path
        self._thumbnail_palette_media_queue = list(
            _THUMBNAIL_PALETTE_MEDIA_TYPES
        )
        self._thumbnail_palette_errors = []
        self._begin_tools_indexing(self.thumbnail_palettes_btn)
        self._thumbnail_palette_poll_timer.start()
        self._start_next_thumbnail_palette_cli()

    def _start_next_thumbnail_palette_cli(self) -> None:
        if not self._thumbnail_palette_media_queue:
            error = "\n\n".join(self._thumbnail_palette_errors) or None
            self._finish_thumbnail_palettes(error)
            return

        if _prefs.get("path") != self._thumbnail_palette_project_path:
            self._finish_thumbnail_palettes(
                "The current project changed before palette generation completed."
            )
            return

        media_type = self._thumbnail_palette_media_queue.pop(0)
        output = tempfile.TemporaryFile(mode="w+b")
        try:
            process = _start_thumbnail_palette_cli(
                self._thumbnail_palette_project_path,
                media_type,
                output,
            )
        except Exception as exc:
            output.close()
            self._thumbnail_palette_errors.append(
                f"{media_type.title()} thumbnail palettes failed:\n\n"
                f"{type(exc).__name__}: {exc}"
            )
            self._start_next_thumbnail_palette_cli()
            return

        self._thumbnail_palette_active_media = media_type
        self._thumbnail_palette_output = output
        self._thumbnail_palette_proc = process

    def _poll_thumbnail_palette_cli(self) -> None:
        process = self._thumbnail_palette_proc
        if process is None:
            return

        returncode = process.poll()
        if returncode is None:
            return

        output_stream = self._thumbnail_palette_output
        output_text = ""
        if output_stream is not None:
            try:
                output_stream.seek(0)
                output_text = output_stream.read().decode(
                    "utf-8", errors="replace",
                ).strip()
            finally:
                output_stream.close()

        media_type = self._thumbnail_palette_active_media
        self._thumbnail_palette_proc = None
        self._thumbnail_palette_output = None
        self._thumbnail_palette_active_media = ""

        if returncode != 0:
            detail = output_text or f"CLI exited with status {returncode}."
            self._thumbnail_palette_errors.append(
                f"{media_type.title()} thumbnail palettes failed:\n\n{detail}"
            )

        self._start_next_thumbnail_palette_cli()

    def _finish_thumbnail_palettes(self, error: str | None = None) -> None:
        self._thumbnail_palette_poll_timer.stop()
        self._finish_tools_indexing()
        self._thumbnail_palette_media_queue = []
        self._thumbnail_palette_errors = []

        if error is not None:
            QMessageBox.critical(self, "Thumbnail Palettes failed", error)
            return

        self._start_project_columns_load(force=True)

    def _on_rebuild_vocabulary(self) -> None:
        if self._tools_indexing():
            return

        project_path = self.path_edit.text().strip()
        if not project_path:
            QMessageBox.warning(
                self, "No Project", "Please set a project folder first.",
            )
            return

        if _prefs.get("path") != project_path:
            _prefs.set("path", project_path)

        output = tempfile.TemporaryFile(mode="w+b")
        try:
            process = _start_vocabulary_cli(project_path, output)
        except Exception as exc:
            output.close()
            QMessageBox.critical(
                self,
                "Vocabulary rebuild failed",
                f"{type(exc).__name__}: {exc}",
            )
            return

        self._vocabulary_project_path = project_path
        self._vocabulary_output = output
        self._vocabulary_proc = process
        self._begin_tools_indexing(self.rebuild_vocabulary_btn)
        self._vocabulary_poll_timer.start()

    def _poll_vocabulary_cli(self) -> None:
        process = self._vocabulary_proc
        if process is None:
            return

        returncode = process.poll()
        if returncode is None:
            return

        output_stream = self._vocabulary_output
        output_text = ""
        if output_stream is not None:
            try:
                output_stream.seek(0)
                output_text = output_stream.read().decode(
                    "utf-8", errors="replace",
                ).strip()
            finally:
                output_stream.close()

        self._vocabulary_proc = None
        self._vocabulary_output = None
        self._vocabulary_project_path = ""
        self._vocabulary_poll_timer.stop()
        self._finish_tools_indexing()

        if returncode != 0:
            detail = output_text or f"CLI exited with status {returncode}."
            QMessageBox.critical(self, "Vocabulary rebuild failed", detail)
            return

        self._start_project_columns_load(force=True)

    def _on_index_illustrations(self) -> None:
        if self._tools_indexing():
            return

        project_path = self.path_edit.text().strip()
        if not project_path:
            QMessageBox.warning(
                self, "No Project", "Please set a project folder first.",
            )
            return

        if _prefs.get("path") != project_path:
            _prefs.set("path", project_path)

        output = tempfile.TemporaryFile(mode="w+b")
        try:
            process = _start_illustration_cli(project_path, output)
        except Exception as exc:
            output.close()
            QMessageBox.critical(
                self,
                "Illustration indexing failed",
                f"{type(exc).__name__}: {exc}",
            )
            return

        self._illustration_output = output
        self._illustration_proc = process
        self._begin_tools_indexing(self.index_illustrations_btn)
        self._illustration_poll_timer.start()

    def _poll_illustration_cli(self) -> None:
        process = self._illustration_proc
        if process is None:
            return

        returncode = process.poll()
        if returncode is None:
            return

        output_stream = self._illustration_output
        output_text = ""
        if output_stream is not None:
            try:
                output_stream.seek(0)
                output_text = output_stream.read().decode(
                    "utf-8", errors="replace",
                ).strip()
            finally:
                output_stream.close()

        self._illustration_proc = None
        self._illustration_output = None
        self._illustration_poll_timer.stop()
        self._finish_tools_indexing()

        if returncode != 0:
            detail = output_text or f"CLI exited with status {returncode}."
            QMessageBox.critical(self, "Illustration indexing failed", detail)
            return

        self._start_project_columns_load(force=True)

    def _on_index_all(self) -> None:
        if self._tools_indexing():
            return

        project_path = self.path_edit.text().strip()
        if not project_path:
            QMessageBox.warning(
                self, "No Project", "Please set a project folder first.",
            )
            return

        if _prefs.get("path") != project_path:
            _prefs.set("path", project_path)

        self._index_all_project_path = project_path
        self._index_all_queue = [
            ("Movie thumbnails", "thumbnail", "movie"),
            ("Gameplay thumbnails", "thumbnail", "gameplay"),
            ("Vocabulary", "vocabulary", None),
            ("Illustrations", "illustration", None),
        ]
        self._index_all_errors = []
        self._begin_tools_indexing(self.index_all_btn)
        self._index_all_poll_timer.start()
        self._start_next_index_all_cli()

    def _start_next_index_all_cli(self) -> None:
        if not self._index_all_queue:
            errors = "\n\n".join(self._index_all_errors) or None
            self._finish_index_all(errors)
            return

        if _prefs.get("path") != self._index_all_project_path:
            self._finish_index_all(
                "The current project changed before indexing completed."
            )
            return

        label, kind, media_type = self._index_all_queue.pop(0)
        output = tempfile.TemporaryFile(mode="w+b")
        try:
            if kind == "thumbnail":
                process = _start_thumbnail_palette_cli(
                    self._index_all_project_path, media_type, output,
                )
            elif kind == "vocabulary":
                process = _start_vocabulary_cli(
                    self._index_all_project_path, output,
                )
            else:
                process = _start_illustration_cli(
                    self._index_all_project_path, output,
                )
        except Exception as exc:
            output.close()
            self._index_all_errors.append(
                f"{label} failed:\n\n{type(exc).__name__}: {exc}"
            )
            self._start_next_index_all_cli()
            return

        self._index_all_active_label = label
        self._index_all_output = output
        self._index_all_proc = process

    def _poll_index_all_cli(self) -> None:
        process = self._index_all_proc
        if process is None:
            return

        returncode = process.poll()
        if returncode is None:
            return

        output_stream = self._index_all_output
        output_text = ""
        if output_stream is not None:
            try:
                output_stream.seek(0)
                output_text = output_stream.read().decode(
                    "utf-8", errors="replace",
                ).strip()
            finally:
                output_stream.close()

        label = self._index_all_active_label
        self._index_all_proc = None
        self._index_all_output = None
        self._index_all_active_label = ""
        if returncode != 0:
            detail = output_text or f"CLI exited with status {returncode}."
            self._index_all_errors.append(f"{label} failed:\n\n{detail}")

        self._start_next_index_all_cli()

    def _finish_index_all(self, error: str | None = None) -> None:
        self._index_all_poll_timer.stop()
        self._finish_tools_indexing()
        self._index_all_project_path = ""
        self._index_all_queue = []
        self._index_all_errors = []

        if error is not None:
            QMessageBox.critical(self, "Indexing failed", error)
            return

        self._start_project_columns_load(force=True)

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
        panel.add_widget(self._build_tools_section())

        inspector.add_tab(panel, "Project")
        tab_bar = inspector.tabbed_panel().tab_bar()
        self._project_inspector_shortcut_label = QLabel("F1", tab_bar)
        self._project_inspector_shortcut_label.setAttribute(
            Qt.WA_TransparentForMouseEvents, True,
        )
        self._project_inspector_shortcut_label.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Fixed,
        )
        self._project_inspector_shortcut_label.setStyleSheet(
            f"background: transparent; color: {theme.TEXT_DIM};"
            f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
            f" font-weight: {theme.WEIGHT_UI}; padding-left: 6px; padding-right: 2px;"
        )
        tab_bar.setTabButton(0, QTabBar.RightSide, self._project_inspector_shortcut_label)

        self._inspector = inspector
        return inspector


    def create_browser(self) -> QWidget:
        from services.corpus_stats import ProjectColumn, PROJECT_COLUMN_IDS_AND_TITLES

        w = QWidget()
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        w.setStyleSheet(f"background: {theme.CANVAS_BG};")

        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_PROJECT_COLUMN_SPACING)

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
            col_widget._datavis_widget.mediaItemActivated.connect(
                self._open_media_item_in_shotlist
            )
            col_widget._datavis_widget.syntheticActivated.connect(
                self._open_untyped_audit
            )
            self._project_column_widgets[col_id] = col_widget
            layout.addWidget(col_widget, 1)

        self._project_columns_worker: Optional[_ProjectColumnsWorker] = None
        self._column_loading_timer = QTimer(self)
        self._column_loading_timer.setInterval(20)
        self._column_loading_timer.timeout.connect(self._tick_column_loading_bars)

        self._start_project_columns_load()
        return w

    def _open_media_item_in_shotlist(self, item: dict) -> None:
        project_path = _prefs.get("path") or ""
        filename = str(item.get("filename") or "")
        media_type = str(item.get("media_type") or "movie")
        if not project_path or not filename:
            return
        from visualizers.shot_visualizer import open_at_shot
        open_at_shot(project_path, filename, media_type=media_type)

    def _open_untyped_audit(self, datavis_kind: str) -> None:
        source = {
            "shot_types": "shot",
            "silhouette_fields": "silhouettes",
        }.get(datavis_kind)
        project_path = _prefs.get("path") or ""
        if not source or not project_path:
            return
        try:
            _start_untyped_audit_cli(project_path, source)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Untyped audit failed",
                f"{type(exc).__name__}: {exc}",
            )

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
            self._project_columns_worker.tier_failed.disconnect(self._on_columns_tier_failed)
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
        worker.tier_failed.connect(self._on_columns_tier_failed)
        self._project_columns_worker = worker
        worker.start()

    def _on_columns_tier_ready(
        self,
        generation,
        columns,
    ) -> None:
        if generation != self._project_load_generation:
            return
        for column in columns:
            widget = self._project_column_widgets.get(column.id)
            if widget is not None:
                widget.set_column(column)
        if all(w.column.state != "loading" for w in self._project_column_widgets.values()):
            self._column_loading_timer.stop()
            self._project_load_state = "loaded"

    def _on_columns_tier_failed(
        self,
        generation,
        tier,
        _message,
    ) -> None:
        if generation != self._project_load_generation:
            return

        from services.corpus_stats import ProjectColumn

        for col_id in _PROJECT_TIER_COLUMN_IDS[tier]:
            widget = self._project_column_widgets[col_id]
            widget.set_column(ProjectColumn(
                id=col_id, title=widget.column.title, count=None,
                datavis={"kind": "empty"}, state="unavailable",
                reason=f"{tier}_tier_error",
            ))
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
