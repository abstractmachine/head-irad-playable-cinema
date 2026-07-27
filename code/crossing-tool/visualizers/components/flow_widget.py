"""Shared flow/grid widget used by browser pages.

This is a minimal extraction of the original flow widget from
`metadata_visualizer.py`. It provides a
deterministic flow layout for a collection of child widgets and exposes
the same public API the browser expects: `set_cells`, `clear_cells`,
`request_reflow`, and `first_row_count`.
"""

from __future__ import annotations

from typing import Optional

from styles import theme
from PyQt5.QtWidgets import QWidget, QLabel, QSizePolicy


class FlowWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cells: list[QWidget] = []
        self._empty_label: QLabel | None = None
        self._first_row_count = 1
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_empty_label(self, label: QLabel) -> None:
        self._empty_label = label
        label.setParent(self)
        label.hide()

    def set_cells(self, cells: list[QWidget]) -> None:
        for cell in self._cells:
            cell.setParent(None)
        self._cells = list(cells)
        for cell in self._cells:
            cell.setParent(self)
            cell.show()
        if self._empty_label is not None:
            self._empty_label.hide()
        self.request_reflow()

    def clear_cells(self) -> None:
        for cell in self._cells:
            cell.setParent(None)
            cell.deleteLater()
        self._cells = []
        self._first_row_count = 1
        if self._empty_label is not None:
            self._empty_label.show()
        self.request_reflow()

    def request_reflow(self) -> None:
        self._do_flow_layout()

    def first_row_count(self) -> int:
        return self._first_row_count

    def _do_flow_layout(self) -> None:
        viewport = self.parentWidget()
        viewport_w = max(1, viewport.width() if viewport is not None else self.width())

        if not self._cells:
            if self._empty_label is not None:
                self._empty_label.setGeometry(self.rect())
                self._empty_label.show()
                self._empty_label.raise_()
            self._first_row_count = 1
            self.setMinimumHeight(1)
            return

        margin = theme.SECTION_GAP
        spacing = theme.SECTION_GAP

        x = margin
        y = margin
        row_h = 0
        first_row_count = 0
        first_row_finalized = False

        for index, cell in enumerate(self._cells):
            cell_w = max(1, cell.sizeHint().width())
            cell_h = max(1, cell.sizeHint().height())

            if x > margin and x + cell_w > viewport_w - margin:
                if not first_row_finalized:
                    first_row_finalized = True
                    first_row_count = index
                x = margin
                y += row_h + spacing
                row_h = 0

            cell.move(x, y)
            cell.resize(cell_w, cell_h)
            x += cell_w + spacing
            row_h = max(row_h, cell_h)

        if not first_row_finalized:
            first_row_count = len(self._cells)
        self._first_row_count = max(1, first_row_count)

        total_h = y + row_h + margin
        if self._empty_label is not None:
            self._empty_label.hide()
        self.setMinimumHeight(max(1, total_h))
