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
import threading
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from visualizers.window_visualizer import WindowVisualizer
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.combo_popup import style_canonical_combo
from visualizers.components.inspector import Inspector
from visualizers.components.metadata_block import (
    MetadataBlock,
    INSPECTOR_ROW_HEIGHT,
    table_key_cell_style,
    table_ui_cell_style,
)
from visualizers.components.sweep_bar import SweepBar
from visualizers.components.tab_panel import TabPanel

from PyQt5.QtCore import Qt, QEvent, QPoint, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLineEdit, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QTabBar, QVBoxLayout, QWidget, QLabel,
)

from tool import prefs as _prefs
from tool.shortcuts import shortcut_label_for
from visualizers.components.shortcut_button import ShortcutButton

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


def _write_error_log(project_path: str, title: str, detail: str) -> Optional[Path]:
    """Write *detail* to ``<project>/outputs/logs/log-<stamp>.txt``.

    Returns the log path, or ``None`` if it could not be written.
    """
    import datetime
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        log_path = Path(project_path) / "outputs" / "logs" / f"log-{stamp}.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"{title}\n{stamp}\n\n{detail}\n", encoding="utf-8")
        return log_path
    except Exception:
        return None


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

    fieldActivated = pyqtSignal(str)

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
        self._hover_frame_style = (
            f"QFrame#VocabularyFieldCell {{ background: {theme.ACCENT}; "
            f"{_project_row_side_style()} }}"
        )
        self.setStyleSheet(self._frame_style)
        self._interactive = False
        self._warning = False
        self._compact_count = False
        self._field = ""
        self._count = ""
        self._visible_content = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(_SPACER_ROW_INSET, 0, _PROJECT_ROW_SEAM_H, 0)
        layout.setSpacing(_SPACER_ROW_GAP)

        self.field_label = QLabel()
        self.field_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.field_label.setFont(font)
        self.field_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._field_style = table_ui_cell_style(
            "", "", include_minimum_height=False,
        )
        self._warning_field_style = table_ui_cell_style(
            "", "", background_color=theme.WARNING_COLOR,
            include_minimum_height=False,
        )
        self._hover_field_style = table_ui_cell_style(
            "", "", background_color=theme.ACCENT, text_color=theme.ACCENT_TEXT,
            include_minimum_height=False,
        )
        self.field_label.setStyleSheet(self._field_style)
        layout.addWidget(self.field_label, 1)

        self.count_label = QLabel()
        self.count_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
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
        self._hover_count_style = table_ui_cell_style(
            "", "", background_color=theme.ACCENT, text_color=theme.ACCENT_TEXT,
            include_minimum_height=False,
            horizontal_padding=_SPACER_ROW_INSET,
        )
        self._compact_hover_count_style = (
            self._hover_count_style
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
        interactive: bool = False,
    ) -> None:
        self._field = field
        self._count = count
        self._visible_content = visible
        self._compact_count = compact_count
        self._warning = warning
        self._interactive = interactive
        self.setCursor(Qt.PointingHandCursor if interactive else Qt.ArrowCursor)
        self._apply_content_style()
        self.count_label.setFixedWidth(count_width)

    def _apply_content_style(self) -> None:
        hovered = self._interactive and self.underMouse()
        self.setStyleSheet(
            self._hover_frame_style if hovered
            else self._warning_frame_style if self._warning
            else self._frame_style
        )
        self.field_label.setStyleSheet(
            self._hover_field_style if hovered
            else self._warning_field_style if self._warning
            else self._field_style
        )
        if hovered:
            count_style = (
                self._compact_hover_count_style
                if self._compact_count else self._hover_count_style
            )
        elif self._warning:
            count_style = (
                self._compact_warning_count_style
                if self._compact_count else self._warning_count_style
            )
        else:
            count_style = (
                self._compact_count_style if self._compact_count else self._count_style
            )
        self.count_label.setStyleSheet(count_style)
        self.field_label.setText(self._field if self._visible_content else "")
        self.count_label.setText(self._count if self._visible_content else "")

    def enterEvent(self, event) -> None:
        self._apply_content_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._apply_content_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if self._interactive and event.button() == Qt.LeftButton:
            self.fieldActivated.emit(self._field)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._interactive and event.button() == Qt.LeftButton:
            self.fieldActivated.emit(self._field)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


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
    fieldActivated = pyqtSignal(str)
    illustrationFieldActivated = pyqtSignal(str, str)
    shotTypeActivated = pyqtSignal(str)

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
            cell.fieldActivated.connect(self._on_field_cell_activated)
            self._field_cells.append(cell)
        for index, cell in enumerate(self._field_cells):
            visible = index < len(self._fields)
            cell.setVisible(visible)
            cell.setAttribute(
                Qt.WA_TransparentForMouseEvents,
                True,
            )
        self._layout_cells()

    def _field_is_interactive(self, item: dict) -> bool:
        if self._word_count_kind == "shot_types":
            return True
        return (
            not item["synthetic"]
            and self._word_count_kind in (
                "vocabulary_fields",
                "silhouette_fields",
                "engraving_fields",
            )
        )

    def _on_field_cell_activated(self, field: str) -> None:
        if self._word_count_kind == "shot_types":
            self.shotTypeActivated.emit(field)
        elif self._word_count_kind == "vocabulary_fields":
            self.fieldActivated.emit(field)
        elif self._word_count_kind in ("silhouette_fields", "engraving_fields"):
            self.illustrationFieldActivated.emit(self._word_count_kind, field)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            for item, cell in zip(self._fields, self._field_cells):
                if not cell.geometry().contains(event.pos()):
                    continue
                if self._word_count_kind == "shot_types":
                    self.shotTypeActivated.emit(item["field"])
                    event.accept()
                    return
                if item["synthetic"]:
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
            interactive = (
                self._field_is_interactive(item)
                and height >= self._minimum_cell_height
                and text_fits
            )
            cell.setAttribute(Qt.WA_TransparentForMouseEvents, not interactive)
            cell.set_content(
                item["field"],
                count,
                row_count_width,
                height >= self._minimum_cell_height and text_fits,
                compact_count=use_intrinsic_count_width,
                warning=item["synthetic"],
                interactive=interactive,
            )
            cell.setGeometry(0, y, self.width(), height)
            y += height
            if index + 1 < gap_count:
                y += gap_heights[index + 1]


_VocabularyDatavisWidget = _WordCountDatavisWidget


class _MediaTitlePopup(QFrame):
    """Fixed-width title popup for a compact Project media cell."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setObjectName("ProjectMediaTitlePopup")
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet(
            f"QFrame#ProjectMediaTitlePopup {{ background: {theme.ACCENT}; "
            f"border: none; }}"
        )

        self._title_label = QLabel(self)
        self._title_label.setWordWrap(True)
        self._title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._title_label.setFont(theme.font_ui())
        self._title_label.setStyleSheet(
            table_ui_cell_style(
                "", "", background_color=theme.ACCENT,
                text_color=theme.ACCENT_TEXT, include_minimum_height=False,
                horizontal_padding=_SPACER_ROW_INSET,
            )
        )
        self.hide()

    def show_for(self, cell, title: str) -> None:
        """Place the title on the open side of *cell*'s datavis column."""
        width = cell.width()
        if width <= 0 or not title:
            self.hide()
            return

        self._title_label.setText(title)
        self._title_label.setFixedWidth(width)
        height = max(
            INSPECTOR_ROW_HEIGHT,
            self._title_label.heightForWidth(width),
        )
        self.setFixedSize(width, height)
        self._title_label.setGeometry(self.rect())

        cell_origin = cell.mapToGlobal(QPoint(0, 0))
        content = cell.parentWidget()
        is_upper_half = (
            content is None
            or cell.y() + cell.height() / 2 < content.height() / 2
        )
        if is_upper_half:
            position = cell_origin + QPoint(0, cell.height() + theme.INSPECTOR_GAP)
        else:
            position = cell_origin - QPoint(0, height + theme.INSPECTOR_GAP)
        self.move(position)
        self.show()
        self.raise_()


class _MediaItemCell(QWidget):
    """One anonymous media item; geometry and color are independent."""

    activated = pyqtSignal(dict)
    titlePopupRequested = pyqtSignal(object)
    titlePopupHidden = pyqtSignal()

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self.item: dict = {}
        self._default_color = color
        self._item_color = color
        self._title = ""
        self._title_is_visible = False
        self._hovered = False
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._title_label = QLabel(self)
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._title_label.setWordWrap(True)
        self._title_label.setFont(theme.font_ui())
        self._title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._title_label.hide()
        self._apply_style()
        self.setCursor(Qt.PointingHandCursor)

    def set_item(self, item: dict) -> None:
        self.item = item
        self._title = str(item.get("title") or "")
        self._title_is_visible = False
        self._hovered = self.underMouse()
        self._title_label.clear()
        self._title_label.hide()
        rgb = item.get("thumbnail_foreground_rgb")
        color = self._default_color
        if (
            item.get("media_type") == "gameplay"
            and isinstance(rgb, list)
            and len(rgb) == 3
            and all(
                isinstance(channel, int)
                and not isinstance(channel, bool)
                and 0 <= channel <= 255
                for channel in rgb
            )
        ):
            color = f"rgb({rgb[0]}, {rgb[1]}, {rgb[2]})"
        self._item_color = color
        self._apply_style()

    def update_title_visibility(self) -> None:
        """Show a title when its complete word-wrapped layout fits the cell."""
        self._title_label.setText(self._title)
        self._title_label.setFixedWidth(self.width())
        required_height = self._title_label.heightForWidth(self.width())
        self._title_is_visible = (
            bool(self._title)
            and self.height() >= INSPECTOR_ROW_HEIGHT
            and required_height <= self.height()
        )
        self._title_label.setText(self._title if self._title_is_visible else "")
        self._title_label.setVisible(self._title_is_visible)
        if self._title_is_visible:
            self.titlePopupHidden.emit()
        self._apply_style()

    def _apply_style(self) -> None:
        hovered = bool(self.item) and self._hovered
        color = theme.ACCENT if hovered else self._item_color
        self.setStyleSheet(f"background: {color}; border: none;")
        self._title_label.setStyleSheet(
            table_ui_cell_style(
                "", "", background_color=color,
                text_color=theme.ACCENT_TEXT if hovered else theme.TEXT,
                include_minimum_height=False,
                horizontal_padding=_SPACER_ROW_INSET,
            )
        )

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._apply_style()
        if not self._title_is_visible:
            self.titlePopupRequested.emit(self)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._apply_style()
        self.titlePopupHidden.emit()
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._title_label.setGeometry(self.rect())
        self.update_title_visibility()
        super().resizeEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.item:
            self.titlePopupHidden.emit()
            self.activated.emit(self.item)
            event.accept()
            return
        super().mousePressEvent(event)


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
        self._media_title_popup = _MediaTitlePopup(self)

    mediaItemActivated = pyqtSignal(dict)

    def set_datavis(self, datavis: dict) -> None:
        self._datavis_kind = datavis.get("kind", "empty")
        self._media_title_popup.hide()
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
                cell.titlePopupRequested.connect(self._show_media_title_popup)
                cell.titlePopupHidden.connect(self._media_title_popup.hide)
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

    def _show_media_title_popup(self, cell: _MediaItemCell) -> None:
        if cell._title_is_visible:
            self._media_title_popup.hide()
            return
        self._media_title_popup.show_for(cell, cell._title)

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
            cell.update_title_visibility()
            y += height

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._media_title_popup.hide()
        super().leaveEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._media_title_popup.hide()
        super().closeEvent(event)


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

    headerActivated = pyqtSignal(str)

    def __init__(self, column, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ProjectColumnWidget")
        self.setStyleSheet(
            f"QWidget#ProjectColumnWidget {{ background: {theme.CELL_BG}; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._header_label = QPushButton()
        self._header_label.setObjectName("ProjectColumnHeader")
        self._header_label.setCursor(Qt.PointingHandCursor)
        self._header_label.setFocusPolicy(Qt.NoFocus)
        self._header_label.setFont(theme.font_ui(bold=True))
        self._header_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._header_label.setStyleSheet(
            f"QPushButton#ProjectColumnHeader {{"
            f" background: {theme.TITLE_BG}; color: {theme.TEXT_DIM};"
            f" border: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER};"
            f" padding: 0px; margin: 0px;"
            f" min-height: {_COLUMN_ROW_H - 2 * _PROJECT_ROW_SEAM_H}px;"
            f" max-height: {_COLUMN_ROW_H - 2 * _PROJECT_ROW_SEAM_H}px;"
            f"}}"
            f"QPushButton#ProjectColumnHeader:hover {{"
            f" background: {theme.ACCENT}; color: {theme.ACCENT_TEXT};"
            f"}}"
            f"QPushButton#ProjectColumnHeader:pressed {{"
            f" background: {theme.BTN_PRESSED}; color: {theme.ACCENT_TEXT};"
            f"}}"
        )
        self._header_label.setFixedHeight(_COLUMN_ROW_H)
        self._header_label.clicked.connect(
            lambda _checked=False: self.headerActivated.emit(self.column.id)
        )
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


class _BackupPathLabel(QLabel):
    def __init__(self):
        super().__init__()
        self._path = ''
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setTextFormat(Qt.PlainText)
        self.setFont(theme.font_ui(bold=True))
        self.setStyleSheet(theme.tooltip_stylesheet())

    def set_path(self, path):
        self._path = path
        self.setToolTip(path)
        self._elide()

    def _elide(self):
        self.setText(self.fontMetrics().elidedText(self._path or '(not set)', Qt.ElideMiddle, self.contentsRect().width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class _BackupWorker(QThread):
    progress = pyqtSignal(object)

    def __init__(self, project, configured, *, mode='preview', preview=None, approve_delete=False, parent=None):
        super().__init__(parent)
        self.project = project
        self.configured = configured
        self.mode = mode
        self.preview = preview
        self.approve_delete = approve_delete
        self.result = None
        self.error = None
        self.was_cancelled = False
        self._cancel = threading.Event()
        self._last_phase = None
        self._last_update = 0.0

    def cancel(self):
        self._cancel.set()

    def _progress(self, event):
        import time
        now = time.monotonic()
        if event['phase'] != self._last_phase or now - self._last_update >= 0.1:
            self.progress.emit(event)
            self._last_phase, self._last_update = event['phase'], now

    def run(self):
        from services import backup_mirror
        try:
            options = {'progress': self._progress, 'cancelled': self._cancel.is_set}
            if self.mode == 'status':
                self.result = backup_mirror.backup_status(self.project, self.configured)
                backup_mirror.checkpoint(None, self._cancel.is_set, 'status')
            elif self.mode == 'verify':
                self.result = backup_mirror.restore_verification(self.project, self.configured, full=True, **options)
                if not self.result.get('full_payload_verified'):
                    raise backup_mirror.MirrorError('Full verification did not complete')
            elif self.mode == 'preview':
                self.result = backup_mirror.prepare_mirror(self.project, self.configured, **options)
            else:
                self.result = backup_mirror.execute_preview(
                    self.project, self.configured, self.preview,
                    approve_delete=self.approve_delete, **options,
                )
        except backup_mirror.MirrorCancelled:
            self.was_cancelled = True
        except Exception as error:
            self.error = str(error)


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

    def __init__(self, project_path, generation: int = 0, parent=None, *, registry=None) -> None:
        super().__init__(parent)
        self.project_path = project_path
        self.generation = generation
        self.registry = registry

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
                columns = getter(self.project_path, **({"registry": self.registry} if self.registry is not None and tier == "cached" else {}))
            except Exception as exc:
                self.tier_failed.emit(
                    self.generation, tier, f"{type(exc).__name__}: {exc}",
                )
            else:
                self.tier_ready.emit(self.generation, columns)


# ---------------------------------------------------------------------------
# Launcher button
# ---------------------------------------------------------------------------


class _LauncherButton(ShortcutButton):
    """Visualizer launcher button whose name and F-key shortcut hint are laid
    out with a stretch so the hint sits flush against the right edge.

    The implementation is shared with every other visualizer that shows a key
    hint on a button — see `visualizers.components.shortcut_button`.
    """

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

    def __init__(self, *, registry=None) -> None:
        self._resource_registry = registry
        self._project_load_generation = 0
        self._project_load_state = "not_started"
        self._backup_worker = None
        self._backup_active = False
        self._backup_preview = None
        self._backup_state = 'idle'
        # Provide pref key to the shell so geometry is saved/restored there
        super().__init__(pref_key="window_project")
        self.setWindowTitle("Crossing — Project")
        self._procs: dict[str, subprocess.Popen] = {}
        self._windows: dict[str, object] = {}  # in-process visualizer windows
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
        self._refresh_backup_status()

    # geometry handled by WindowVisualizer (pref_key passed at construction)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._shown_as_project = True

    def closeEvent(self, event) -> None:
        if self._backup_active:
            if self._backup_worker is not None:
                self._backup_worker.cancel()
            self.backup_status.setText("Stopping at a safe checkpoint; close again when stopped.")
            self.backup_status.show()
            event.ignore()
            return
        self._backup_preview = None
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
            self._refresh_backup_button()
            self._refresh_backup_status()

    # ------------------------------------------------------------------
    # Backup path

    def _build_backup_section(self) -> CollapsibleSection:
        sec = CollapsibleSection("Backup", pref_key="project_section_backup")
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(theme.SECTION_GAP)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SECTION_GAP)
        self.backup_browse_btn = QPushButton("Path")
        self.backup_browse_btn.setToolTip("Select the configured backup destination")
        self.backup_browse_btn.clicked.connect(self._on_backup_browse)
        self.backup_browse_btn.setStyleSheet(theme.action_button_stylesheet())
        row.addWidget(self.backup_browse_btn)
        self.backup_path_edit = _BackupPathLabel()
        self.backup_path_edit.set_path(_prefs.get("backup_path") or "")
        row.addWidget(self.backup_path_edit, 1)
        outer.addLayout(row)

        self.backup_btn = QPushButton("Mirror")
        self.backup_btn.setToolTip("Prepare and confirm project synchronization")
        self.backup_btn.clicked.connect(self._on_backup_run)
        self.backup_btn.setStyleSheet(theme.action_button_stylesheet())
        self.backup_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.backup_verify_btn = QPushButton("Verify")
        self.backup_verify_btn.setToolTip("Fully verify the existing mirror; this can take several hours")
        self.backup_verify_btn.clicked.connect(self._on_backup_verify)
        self.backup_verify_btn.setStyleSheet(theme.action_button_stylesheet())
        self.backup_verify_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(0, 0, 0, 0)
        buttons_row.setSpacing(theme.SECTION_GAP)
        buttons_row.addWidget(self.backup_btn, 1)
        buttons_row.addWidget(self.backup_verify_btn, 1)
        outer.addLayout(buttons_row)

        self.backup_status = QLabel()
        self.backup_status.setTextFormat(Qt.PlainText)
        self.backup_status.setWordWrap(True)
        self.backup_status.setFont(theme.font_mono())
        self.backup_status.hide()
        self.backup_cancel_btn = QPushButton("Cancel")
        self.backup_cancel_btn.setStyleSheet(theme.action_button_stylesheet())
        self.backup_cancel_btn.clicked.connect(self._cancel_backup)
        self.backup_cancel_btn.setToolTip("Stop at a safe checkpoint; a large file can delay stopping")
        self.backup_cancel_btn.hide()
        information = QHBoxLayout()
        information.setSpacing(theme.SECTION_GAP)
        information.addWidget(self.backup_status, 1)
        information.addWidget(self.backup_cancel_btn, 0, Qt.AlignTop)
        outer.addLayout(information)
        self.backup_history = MetadataBlock(['Last mirror', 'Last full verify'])
        outer.addWidget(self.backup_history)
        self._set_backup_history({})

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
            _prefs.get("backup_path") or str(Path.home()),
        )
        if folder:
            _prefs.set("backup_path", folder)
            self._backup_preview = None
            self._backup_state = 'idle'
            self.backup_status.hide()
            self._refresh_backup_button()
            self._refresh_backup_status()

    def _refresh_backup_button(self) -> None:
        ready = bool(_prefs.get("backup_path") and _prefs.get("path"))
        self.backup_path_edit.set_path(_prefs.get("backup_path") or "")
        self.backup_btn.setEnabled(ready and not self._backup_active)
        self.backup_verify_btn.setEnabled(ready and not self._backup_active and self._backup_preview is None)
        self.backup_btn.setText('Confirm' if self._backup_preview is not None else 'Mirror')
        self.backup_verify_btn.setText('Verify')

    def _set_backup_history(self, status):
        from datetime import datetime
        def date(value, fallback):
            return datetime.fromtimestamp(value).strftime('%d %b %Y, %H:%M') if value else fallback
        self.backup_history.set('Last mirror', date(status.get('last_mirror'), 'Unknown'))
        verified = date(status.get('last_full_verify'), 'Not verified')
        if status.get('last_full_verify') and not status.get('verification_current'):
            verified += ' (previous mirror)'
        self.backup_history.set('Last full verify', verified)

    def _refresh_backup_status(self):
        if self._backup_active:
            return
        self._set_backup_history({})
        if _prefs.get('path') and _prefs.get('backup_path'):
            self._backup_idle_labels = (self.backup_btn.text(), self.backup_verify_btn.text())
            self._backup_project = _prefs.get('path')
            self._backup_configured = _prefs.get('backup_path')
            self._start_backup_worker(mode='status')

    def _on_backup_run(self) -> None:
        if self._backup_active:
            return
        if self._backup_preview is not None:
            preview = self._backup_preview
            deleting = any(preview['actions'].get(action, {}).get('count', 0) for action in ('delete', 'rmdir'))
            if deleting:
                from services import backup_mirror
                if QMessageBox.warning(self, "Authorize Deletion", backup_mirror.preview_text(preview) + "\n\nAdditionally authorize these deletions?",
                                       QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                    return
            self._backup_preview = None
            self._start_backup_worker(mode='mirror', preview=preview, approve_delete=deleting)
            return
        project_path = _prefs.get("path")
        if not project_path:
            QMessageBox.warning(self, "No Project", "Please set a project folder first.")
            return
        self._backup_project = project_path
        self._backup_configured = _prefs.get("backup_path")
        self._start_backup_worker()

    def _on_backup_verify(self):
        if self._backup_active or self._backup_preview is not None:
            return
        self._backup_project = _prefs.get('path')
        self._backup_configured = _prefs.get('backup_path')
        if self._backup_project and self._backup_configured:
            self._start_backup_worker(mode='verify')

    def _cancel_backup(self):
        if self._backup_worker is not None:
            self._backup_worker.cancel()
            self.backup_cancel_btn.setEnabled(False)
            self.backup_status.setText('Stopping at a safe checkpoint...')
        else:
            self._backup_preview = None
            self._finish_backup('Cancelled: backup unchanged.')

    def _start_backup_worker(self, preview=None, approve_delete=False, mode='preview'):
        self._backup_active = True
        self._backup_state = mode
        self._backup_worker = _BackupWorker(
            self._backup_project, self._backup_configured, preview=preview,
            mode=mode, approve_delete=approve_delete, parent=self,
        )
        self._backup_worker.progress.connect(self._on_backup_progress)
        self._backup_worker.finished.connect(self._on_backup_finished)
        self.backup_browse_btn.setEnabled(False)
        self.project_browse_btn.setEnabled(False)
        self._refresh_backup_button()
        if mode != 'status':
            self.backup_btn.setText('Mirroring\u2026' if mode == 'mirror' else 'Preparing\u2026' if mode == 'preview' else 'Mirror')
            self.backup_verify_btn.setText('Preparing\u2026' if mode == 'verify' else 'Verify')
            self.backup_status.setText('Preparing...')
            self.backup_status.setToolTip('')
            self.backup_status.show()
            self.backup_cancel_btn.show()
            self.backup_cancel_btn.setEnabled(True)
            self._backup_loading_bar.start()
            self._backup_loading_timer.start()
        self._backup_worker.start()

    def _on_backup_progress(self, event):
        detail = event.get('path', event.get('entries', ''))
        if 'files_verified' in event:
            checked, total = event['files_verified'], event.get('total_files')
            detail = f"{checked:,}" + (f" / {total:,} files ({100 * checked / total:.1f}%)" if total else ' files checked') + f"; SQLite: {event.get('sqlite_databases_verified', 0):,}; {detail}"
        if self._backup_state == 'verify' and event['phase'] not in ('preparing', 'preflight'):
            self.backup_verify_btn.setText('Verifying\u2026')
        self.backup_status.setText(f"{event['phase'].replace('_', ' ').capitalize()}: {detail}")

    def _finish_backup(self, status):
        self._backup_active = False
        self._backup_state = 'idle'
        self._backup_loading_timer.stop()
        self._backup_loading_bar.stop()
        self.backup_browse_btn.setEnabled(True)
        self.project_browse_btn.setEnabled(True)
        self._refresh_backup_button()
        self.backup_status.setText(status)
        self.backup_status.setToolTip('')
        self.backup_status.setVisible(bool(status))
        self.backup_cancel_btn.hide()

    def _on_backup_finished(self):
        from services import backup_mirror
        worker = self._backup_worker
        self._backup_worker = None
        worker.deleteLater()
        if worker.was_cancelled or (worker.mode in ('preview', 'status') and worker._cancel.is_set()):
            self._finish_backup("Cancelled. Any installed files retain recovery journals; no verified completion.")
            if worker.mode in ('mirror', 'verify'):
                self._refresh_backup_status()
        elif worker.error is not None:
            self._finish_backup("Backup stopped: " + worker.error)
            (self.backup_verify_btn if worker.mode == 'verify' else self.backup_btn).setText('Retry')
            if worker.mode in ('mirror', 'verify'):
                self._refresh_backup_status()
        elif worker.mode == 'status':
            self._backup_active = False
            self._backup_state = 'idle'
            self.backup_browse_btn.setEnabled(True)
            self.project_browse_btn.setEnabled(True)
            self._refresh_backup_button()
            self.backup_btn.setText(self._backup_idle_labels[0])
            self.backup_verify_btn.setText(self._backup_idle_labels[1])
            self._set_backup_history(worker.result)
        elif worker.mode in ('mirror', 'verify'):
            if worker.mode == 'verify':
                self._finish_backup(f"Done: full verification completed; {worker.result['verification']['files_verified']:,} files checked.")
            else:
                self._finish_backup(f"Done: {worker.result['files_verified']:,} files verified.")
            self._refresh_backup_status()
        else:
            preview = worker.result
            actions = preview['actions']
            count = lambda action: actions.get(action, {}).get('count', 0)
            summary = (f"Copy: {count('copy'):,}; update: {count('update'):,}; delete: {count('delete'):,} files, {count('rmdir'):,} directories\n"
                       f"Transfer: {preview['bytes_to_transfer']:,} bytes; available: {preview['available_bytes']:,} bytes\n"
                       f"Deletion: {preview['estimated_delete_recovery_bytes']:,} bytes")
            if preview['context']['destination'] != self._backup_configured:
                summary += '\nDestination: ' + preview['context']['destination']
            summary += ''.join('\nBLOCKED: ' + message for message in preview['apply_blocked'])
            self._finish_backup(summary)
            self.backup_status.setToolTip(backup_mirror.preview_text(preview))
            if not preview['apply_blocked']:
                self._backup_preview = preview
                self._refresh_backup_button()
                self.backup_browse_btn.setEnabled(False)
                self.project_browse_btn.setEnabled(False)
                self.backup_cancel_btn.setEnabled(True)
                self.backup_cancel_btn.show()
            else:
                self.backup_btn.setText('Retry')

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

    def _report_tools_failure(self, title: str, detail: str) -> None:
        """Write indexing output to a log file and open it in the default viewer.

        Falls back to a dialog when no log file could be written or opened.
        """
        project_path = self.path_edit.text().strip() or _prefs.get("path") or ""
        log_path = _write_error_log(project_path, title, detail) if project_path else None
        if log_path is not None:
            try:
                subprocess.Popen(["xdg-open", str(log_path)])
                return
            except Exception:
                pass
        QMessageBox.critical(self, title, detail)

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
            self._report_tools_failure("Thumbnail Palettes failed", error)
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
            self._report_tools_failure("Vocabulary rebuild failed", detail)
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
            self._report_tools_failure("Illustration indexing failed", detail)
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
            self._report_tools_failure("Indexing failed", error)
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
            col_widget._datavis_widget.fieldActivated.connect(
                self._open_vocabulary_field
            )
            col_widget._datavis_widget.illustrationFieldActivated.connect(
                self._open_illustration_field
            )
            col_widget._datavis_widget.shotTypeActivated.connect(
                self._open_shot_type
            )
            col_widget.headerActivated.connect(self._open_column_visualizer)
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

    def _open_column_visualizer(self, column_id: str) -> None:
        """Open the visualizer represented by a Project dashboard header."""
        if column_id == "movies":
            self._launch("metadata", media_type="movie")
        elif column_id == "gameplay":
            self._launch("metadata", media_type="gameplay")
        elif column_id == "shots":
            self._launch("shotlist")
        elif column_id == "vocabulary":
            self._launch("mosaic")
        elif column_id in ("silhouettes", "engravings"):
            self._launch("illustration", source_tab=column_id)

    def _open_vocabulary_field(self, field: str) -> None:
        """Open Mosaic and select the exact Project vocabulary field."""
        if field:
            self._launch("mosaic", field=field)

    def _open_shot_type(self, shot_type: str) -> None:
        """Open Mosaic at an exact Project shot type across both media types."""
        if shot_type:
            self._launch(
                "mosaic",
                media_type="--all",
                shot_type=shot_type,
            )

    def _open_illustration_field(self, datavis_kind: str, field: str) -> None:
        """Open Illustration at the clicked aggregate field across both media types."""
        source_tab = {
            "silhouette_fields": "silhouettes",
            "engraving_fields": "engravings",
        }.get(datavis_kind)
        if not source_tab or not field:
            return
        from services.illustration_index import ALL_MEDIA

        self._launch(
            "illustration",
            media_type=ALL_MEDIA,
            source_tab=source_tab,
            field=field,
        )

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
        worker = _ProjectColumnsWorker(_prefs.get("path"), generation, registry=self._resource_registry)
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

    def _launch(
        self,
        subcommand: str,
        *,
        media_type: str = "movie",
        source_tab: str | None = None,
        field: str | None = None,
        shot_type: str | None = None,
    ) -> None:
        if not _prefs.get("path"):
            QMessageBox.warning(self, "No Project", "Please set a project folder first.")
            return

        # Raise an already-open in-process window (works because all visualizers
        # opened via this launcher share the same QApplication event loop).
        existing_window = self._windows.get(subcommand)
        if subcommand == "metadata" and existing_window is not None:
            try:
                existing_window.select_media_type(media_type)
            except RuntimeError:
                self._windows.pop(subcommand, None)
        elif subcommand == "mosaic" and existing_window is not None:
            try:
                existing_window.select_media_type(media_type)
                if field:
                    existing_window.select_field(field, preserve_missing=True)
                if shot_type:
                    existing_window.select_shot_type(shot_type)
            except RuntimeError:
                self._windows.pop(subcommand, None)

        from visualizers._window_helpers import raise_existing_window
        if subcommand != "illustration" and raise_existing_window(subcommand):
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
            win = self._create_in_process_window(
                subcommand,
                project_path,
                media_type=media_type,
                source_tab=source_tab,
                field=field,
                shot_type=shot_type,
            )
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
                if _ipc_socket_path(project_path, registry=self._resource_registry).exists():
                    ipc_send_load(project_path, "", "movie", **({"registry": self._resource_registry} if self._resource_registry is not None else {}))
                    return
            except Exception:
                pass

        proc = self._procs.get(subcommand)
        if proc is not None and proc.poll() is None:
            return  # already running, nothing more we can do without OS tools

        command = [sys.executable, str(_CLI_PATH), "visualizer", subcommand]
        if self._resource_registry is not None and subcommand in ("project", "cloud", "sync", "segmentation", "shotlist", "mosaic", "flipbook"):
            command += ["--resource-registry", str(self._resource_registry)]
        self._procs[subcommand] = subprocess.Popen(command)

    def _create_in_process_window(
        self,
        subcommand: str,
        project_path: str,
        *,
        media_type: str = "movie",
        source_tab: str | None = None,
        field: str | None = None,
        shot_type: str | None = None,
    ):
        """Instantiate the named visualizer as a window inside this process.

        Returns the window (not yet shown) or None for subcommands that must
        run as separate processes (e.g. shotlist with its own IPC server).
        """
        registry = getattr(self, "_resource_registry", None)
        context = {"registry": registry} if registry is not None else {}
        if subcommand == "metadata":
            from visualizers.metadata_visualizer import MetadataVisualizer
            return MetadataVisualizer(project_path, media_type=media_type)
        elif subcommand == "cloud":
            from visualizers.cloud_visualizer import CloudVisualizer
            return CloudVisualizer(project_path, **context)
        elif subcommand == "mosaic":
            from visualizers.mosaic_visualizer import MosaicVisualizer
            return MosaicVisualizer(
                project_path,
                media_type=media_type,
                shot_type=shot_type,
                initial_field=field,
                **context,
            )
        elif subcommand == "book":
            from visualizers.book_visualizer import BookVisualizerWindow
            return BookVisualizerWindow(project_path)
        elif subcommand == "illustration":
            # Per the new architecture, never instantiate another visualizer's
            # QWidget hierarchy inside this process. Instead request navigation
            # via the Illustration helper which will try IPC and otherwise
            # delegate process creation to the canonical launcher.
            from visualizers.illustration_visualizer import open_at_illustration
            open_at_illustration(
                project_path,
                media_type=media_type,
                field=field,
                source_tab=source_tab or "silhouettes",
            )
            return None
        elif subcommand == "palette":
            from visualizers.palette_visualizer import PaletteVisualizerWindow
            return PaletteVisualizerWindow(project_path, media_type=media_type)
        elif subcommand == "flipbook":
            import visualizers.flipbook_visualizer as _fv
            _fv._FONT_FAMILY = _fv._load_flipbook_font()
            return _fv.FlipbookVisualizerWindow(project_path, media_type=media_type, **context)
        elif subcommand == "sync":
            from visualizers.sync_visualizer import SyncVisualizerWindow
            return SyncVisualizerWindow(**context)
        elif subcommand == "segmentation":
            model_name = _prefs.get("model_segmentation", "sam3.pt") or "sam3.pt"
            from visualizers.segmentation_visualizer import SAMExplorer
            return SAMExplorer(project_path, model_name=model_name, **context)
        return None  # caller falls through to subprocess

    # Keyboard handled by WindowVisualizer (Tab/Shift+Tab/Esc/Ctrl+Q/Ctrl+W)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer(*, registry=None) -> None:
    """Create QApplication (if needed) and launch the project window."""
    from visualizers.launcher import run_visualizer_window
    run_visualizer_window("project", lambda: ProjectVisualizer(registry=registry), check_existing=False)


if __name__ == "__main__":
    run_visualizer()
