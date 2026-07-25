"""MetadataBlock — reusable key-value presentation block.

A fixed row set updated via ``set(key, value)`` and reset via ``clear()``.
Used inside inspector sections to keep metadata presentation consistent across
visualizers.

Example::

    block = MetadataBlock(["label", "film", "frame", "confidence"])
    block.set("label", "horse")
    block.set("film", "Django (1966)")
    block.set("frame", "1275")
    block.set("confidence", "0.91")
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QBrush, QColor, QPalette
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from styles import theme
from styles.theme import JumpScrollBar


INSPECTOR_ROW_HEIGHT = 24
INSPECTOR_DIVIDER_THICKNESS = theme.INSPECTOR_GAP

# Backward-compatible aliases for existing imports.
TABLE_ROW_H = INSPECTOR_ROW_HEIGHT
TABLE_BORDER = INSPECTOR_DIVIDER_THICKNESS


def table_row_edges(row_idx: int, last_idx: int) -> tuple[str, str]:
    # Section owns header band. The QGridLayout's vertical spacing (TAB_BG
    # showing through) provides inter-row separators for MetadataBlock.
    # For QTableWidget-based tables (Layers), the bottom border drives the
    # row separator on every row.
    top = ""
    bottom = ""
    # Add a bottom divider between rows (but not after the last row) so the
    # seam color (TAB_BG) appears only between cells, not around the table.
    if row_idx < last_idx:
        bottom = f"border-bottom: {INSPECTOR_DIVIDER_THICKNESS}px solid {theme.TAB_BG};"
    return top, bottom


def table_key_cell_style(top: str, bottom: str) -> str:
    return (
        f"background: {theme.CELL_BG}; color: {theme.TEXT_DIM};"
        f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
        f" font-weight: {theme.WEIGHT_UI};"
        f"{top}{bottom}"
        f" border-right: {INSPECTOR_DIVIDER_THICKNESS}px solid {theme.TAB_BG};"
        f" min-height: {INSPECTOR_ROW_HEIGHT}px;"
        f" padding: 0px;"
    )


def table_value_cell_style(top: str, bottom: str) -> str:
    return (
        f"background: {theme.CELL_BG}; color: {theme.TEXT};"
        f" font-family: '{theme.FAMILY_MONO}';"
        f" font-size: {theme.BASE_PT}pt;"
        f" font-weight: {theme.WEIGHT_MONO};"
        f"{top}{bottom} min-height: {INSPECTOR_ROW_HEIGHT}px;"
        f" padding: 0px;"
    )


def table_widget_style() -> str:
    """Return the canonical chrome style for table-based inspector widgets."""
    return (
        f"background: transparent;"
        f" border: none;"
        f" margin: 0px;"
        f" padding: 0px;"
    )


def table_widget_item_style() -> str:
    """Return the canonical item style for table-based inspector rows."""
    return (
        f"background: {theme.CELL_BG};"
        f" border: none;"
        f" padding: 0px {theme.INSPECTOR_GAP}px 0px 3px;"
    )


def table_action_cell_style(selected: bool, add_left_divider: bool = False) -> str:
    """Return canonical style for centered action controls inside table cells."""
    bg = theme.ACCENT if selected else theme.CELL_BG
    return (
        f"QWidget {{ background: {bg}; }}"
        f" QPushButton {{ background: transparent; border: none; }}"
        f" QPushButton:hover {{ background: {theme.ACCENT}; }}"
        f" QPushButton:pressed {{ background: {theme.BTN_PRESSED}; }}"
        f" QPushButton:checked {{ background: transparent; border: none; }}"
    )


def inspector_action_icon_size() -> int:
    # Use the canonical inspector row height minus a small margin so the icon
    # fills the cell visually without clipping.
    return max(12, INSPECTOR_ROW_HEIGHT - 6)


def inspector_action_button_size() -> int:
    return max(14, INSPECTOR_ROW_HEIGHT - 4)


def inspector_action_button_style() -> str:
    return (
        f"QPushButton {{ background: transparent; border: none; border-radius: 0px; padding: 0px;"
        f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt; font-weight: {theme.WEIGHT_UI}; }}"
        f"QPushButton:hover {{ background: {theme.ACCENT}; }}"
        f"QPushButton:pressed {{ background: {theme.BTN_PRESSED}; }}"
    )


class InspectorTable(QWidget):
    """Canonical inspector table implementation shared by Info/Layers."""

    def __init__(self, column_count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._table = QTableWidget(0, column_count)
        self._table.setFrameShape(QTableWidget.NoFrame)
        self._table.setLineWidth(0)
        self._table.setMidLineWidth(0)
        # Keep the table viewport transparent so the TabPanel surface shows
        # through as the single pane background.  Use a transparent viewport
        # stylesheet rather than altering the palette so all platforms behave
        # consistently.
        try:
            self._table.viewport().setStyleSheet("background: transparent;")
        except Exception:
            pass
        self._table.setFocusPolicy(Qt.NoFocus)
        # Disable the native grid so only the internal seam styles we add
        # via cell CSS are visible (no outer perimeter/gridlines).
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        try:
            self._table.setVerticalScrollBar(JumpScrollBar())
        except Exception:
            pass
        self._table.setCornerButtonEnabled(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)

        hh = self._table.horizontalHeader()
        hh.hide()
        hh.setStretchLastSection(False)

        vh = self._table.verticalHeader()
        vh.hide()
        vh.setDefaultSectionSize(INSPECTOR_ROW_HEIGHT)
        vh.setSectionResizeMode(QHeaderView.Fixed)

        self._table.setStyleSheet(
            f"QTableWidget {{ {table_widget_style()} }}"
            f"QTableWidget::item {{ {table_widget_item_style()} }}"
            f"QTableWidget::item:selected {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
        )
        # Table wrapper should not paint a background — the TabPanel owns
        # the pane background; keep wrapper transparent and edge-to-edge.
        try:
            self.setStyleSheet("background: transparent; border: none;")
        except Exception:
            pass
        outer.addWidget(self._table)

    def table(self) -> QTableWidget:
        return self._table

    def set_column_resize_mode(self, column: int, mode: QHeaderView.ResizeMode) -> None:
        self._table.horizontalHeader().setSectionResizeMode(column, mode)

    def set_column_width(self, column: int, width: int) -> None:
        self._table.setColumnWidth(column, width)

    def set_horizontal_scrollbar_policy(self, policy: Qt.ScrollBarPolicy) -> None:
        self._table.setHorizontalScrollBarPolicy(policy)

    def set_vertical_scrollbar_policy(self, policy: Qt.ScrollBarPolicy) -> None:
        self._table.setVerticalScrollBarPolicy(policy)

    def set_row_count(self, row_count: int) -> None:
        self._table.setRowCount(row_count)

    def row_count(self) -> int:
        return self._table.rowCount()

    def insert_row(self, row: int) -> None:
        self._table.insertRow(row)
        self._table.setRowHeight(row, INSPECTOR_ROW_HEIGHT)

    def remove_row(self, row: int) -> None:
        self._table.removeRow(row)

    def set_item(self, row: int, column: int, item: QTableWidgetItem) -> None:
        self._table.setItem(row, column, item)

    def item(self, row: int, column: int) -> QTableWidgetItem | None:
        return self._table.item(row, column)

    def set_cell_widget(self, row: int, column: int, widget: QWidget) -> None:
        self._table.setCellWidget(row, column, widget)

    def cell_widget(self, row: int, column: int) -> QWidget | None:
        return self._table.cellWidget(row, column)

    def make_cell(self, child: QWidget, alignment: Qt.Alignment = Qt.AlignCenter) -> QWidget:
        cell = QWidget()
        try:
            cell.setStyleSheet("background: transparent; margin: 0px; padding: 0px;")
        except Exception:
            pass
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(child, 0, alignment)
        return cell

    def configure_interactive_rows(self) -> None:
        """Enable selection/edit/reorder behavior for interactive table use cases."""
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self._table.setDragEnabled(True)
        self._table.setAcceptDrops(True)
        self._table.viewport().setAcceptDrops(True)
        self._table.setDragDropOverwriteMode(False)
        self._table.setDragDropMode(QAbstractItemView.InternalMove)
        self._table.setDefaultDropAction(Qt.MoveAction)
        self._table.setDropIndicatorShown(True)

    def make_action_button(self, icon_name: str, tooltip: str, normal_color: str = theme.TEXT_DIM) -> tuple[QWidget, object]:
        """Create a canonical action button that fills its table cell."""
        from visualizers.components.hover_icon_button import HoverIconButton, build_icon_pair

        icon_size = inspector_action_icon_size()
        normal, hover = build_icon_pair(icon_name, icon_size, normal_color=normal_color)
        btn = HoverIconButton("", normal, hover)
        btn.setIconSize(QSize(icon_size, icon_size))
        # No setFixedSize — let the cell layout expand the button to fill the cell.
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        btn.setStyleSheet(inspector_action_button_style())
        btn.setToolTip(tooltip)

        # Stretch button to fill cell — no AlignCenter so it expands fully.
        cell = QWidget()
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(btn)
        return cell, btn

    def style_action_cell(self, cell: QWidget, selected: bool, add_left_divider: bool = True) -> None:
        cell.setStyleSheet(table_action_cell_style(selected, add_left_divider=add_left_divider))

    def style_text_item(self, item: QTableWidgetItem, selected: bool, dimmed: bool = False) -> None:
        text_color = theme.ACCENT_TEXT if selected else (theme.TEXT_DIM if dimmed else theme.TEXT)
        bg_color = theme.ACCENT if selected else theme.CELL_BG
        item.setForeground(QBrush(QColor(text_color)))
        item.setBackground(QBrush(QColor(bg_color)))
        font = item.font()
        font.setItalic(dimmed)
        item.setFont(font)
        item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)


class MetadataBlock(QWidget):
    """Fixed key-value row grid for inspector presentation.

    Uses QGridLayout directly — this is the golden-reference implementation
    for the inspector table visual contract. Do not route Info through
    InspectorTable/QTableWidget as that inflates row heights.

    This widget presents metadata only. It does not own selection, data
    retrieval, or project operations.

    Parameters
    ----------
    rows:
        Ordered list of key names.  Each key gets one row: a right-aligned
        dim label on the left and a white value label on the right.
    parent:
        Optional parent widget.
    """

    def __init__(self, rows: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Wrapper should be transparent and borderless so TabPanel paints
        # the canonical pane background; keep internal seams only.
        try:
            self.setStyleSheet("background: transparent; border: none;")
        except Exception:
            pass
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(0)
        # No vertical spacing between rows so cells touch edge-to-edge.
        layout.setVerticalSpacing(0)
        layout.setColumnStretch(0, 0)
        layout.setColumnStretch(1, 1)

        self._rows = list(rows)
        self._labels: dict[str, QLabel] = {}
        last_idx = len(self._rows) - 1
        for row_idx, key in enumerate(self._rows):
            top, bottom = table_row_edges(row_idx, last_idx)

            key_lbl = QLabel(key)
            key_lbl.setStyleSheet(table_key_cell_style(top, bottom))
            key_lbl.setAlignment(Qt.AlignRight | Qt.AlignTop)
            key_lbl.setMinimumHeight(INSPECTOR_ROW_HEIGHT)
            layout.addWidget(key_lbl, row_idx, 0)

            val_lbl = QLabel("—")
            val_lbl.setStyleSheet(table_value_cell_style(top, bottom))
            val_lbl.setWordWrap(True)
            val_lbl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            val_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            sp.setHeightForWidth(True)
            val_lbl.setSizePolicy(sp)
            val_lbl.setMinimumHeight(INSPECTOR_ROW_HEIGHT)
            layout.addWidget(val_lbl, row_idx, 1)

            self._labels[key] = val_lbl

    def labels(self) -> dict[str, QLabel]:
        """Return the mutable key → value-label mapping used by callers."""
        return dict(self._labels)

    def set(self, key: str, value: str) -> None:
        """Set the displayed value for *key*.  No-op if *key* is unknown."""
        if key in self._labels:
            self._labels[key].setText(value)

    def clear(self) -> None:
        """Reset all rows to the placeholder dash (—)."""
        for lbl in self._labels.values():
            lbl.setText("—")
