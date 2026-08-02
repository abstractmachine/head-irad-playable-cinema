"""AspectGridWidget — shared best-fit grid layout for uniform-aspect cells.

This is a minimal extraction of the grid-reflow algorithm that used to be
implemented twice, near-identically, as a private ``_GridWidget`` inside
both ``palette_visualizer.py`` and ``flipbook_visualizer.py``. Both browse
a single movie's worth of same-size, fixed-aspect-ratio cells (colour
swatches / page thumbnails) and want the column count that maximizes
on-screen cell area within the available viewport.

`AspectGridWidget` also owns an optional zoom multiplier so it can be
driven by the shared `ZoomManager` the way Metadata's `FlowWidget` is:
at the default zoom (1.0) the layout is byte-for-byte the original
best-fit computation (no scrolling); away from 1.0 the baseline cell
size is scaled and the column count is recomputed so cells never
overflow the viewport width — extra rows (and a wrapping QScrollArea)
absorb the zoom instead, mirroring `FlowWidget`/Metadata's zoom model.
"""

from __future__ import annotations

import math
from typing import List, Optional

from PyQt5.QtWidgets import QWidget, QSizePolicy


class AspectGridWidget(QWidget):
    def __init__(
        self,
        aspect: float = 16 / 9,
        gap: int = 4,
        margin: int = 10,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._cells: List[QWidget] = []
        self._aspect = aspect
        self._gap = gap
        self._margin = margin
        self._zoom = 1.0
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------ cells
    def set_cells(self, cells: List[QWidget]) -> None:
        for cell in self._cells:
            cell.setParent(None)
        self._cells = list(cells)
        for cell in self._cells:
            cell.setParent(self)
            cell.show()
        self.request_reflow()

    def clear_cells(self) -> None:
        for cell in self._cells:
            cell.setParent(None)
            cell.deleteLater()
        self._cells = []
        self.request_reflow()

    def cells(self) -> List[QWidget]:
        return list(self._cells)

    # ------------------------------------------------------------------ zoom
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float) -> None:
        self._zoom = zoom
        self.request_reflow()

    # ------------------------------------------------------------------ layout
    def request_reflow(self) -> None:
        self._reflow()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self) -> None:
        n = len(self._cells)
        if n == 0:
            self.setMinimumHeight(1)
            return

        # Use the scroll-area viewport (our parent once embedded) for the
        # available area, mirroring FlowWidget's technique — this stays the
        # *visible* viewport size even once zoom has made our own height
        # taller than it (and thus scrollable).
        parent = self.parentWidget()
        avail_w = parent.width() if parent is not None else self.width()
        avail_h = parent.height() if parent is not None else self.height()
        W = max(1, avail_w - 2 * self._margin)
        H = max(1, avail_h - 2 * self._margin)

        # Baseline (zoom == 1.0): the column count that maximises cell area
        # while fitting every cell inside W x H at a fixed aspect ratio.
        # This is the original Palette/Flipbook algorithm, unchanged.
        best_area = 0.0
        best_cols = 1
        best_cw = 0.0
        best_ch = 0.0
        for cols in range(1, n + 1):
            rows = math.ceil(n / cols)
            cw = (W - (cols - 1) * self._gap) / cols
            ch = cw / self._aspect
            if rows * ch + (rows - 1) * self._gap > H:
                ch = (H - (rows - 1) * self._gap) / rows
                cw = ch * self._aspect
            if cw <= 0 or ch <= 0:
                continue
            area = cw * ch
            if area > best_area:
                best_area, best_cols, best_cw, best_ch = area, cols, cw, ch

        if best_area <= 0:
            return

        if abs(self._zoom - 1.0) < 1e-6:
            # Identical to the pre-zoom behavior: no scrolling, grid is
            # centered in the available area.
            cols = best_cols
            cell_w = max(1, int(best_cw))
            cell_h = max(1, int(best_ch))
        else:
            # Scale the baseline cell width by zoom, then recompute how
            # many columns of that size fit across W so cells never
            # overflow horizontally — extra rows (and vertical scrolling)
            # absorb zooming in, exactly like FlowWidget/ZoomManager.
            target_cw = max(1.0, best_cw * self._zoom)
            cols = max(1, min(n, int((W + self._gap + 1e-6) // (target_cw + self._gap))))
            cell_w = max(1, int((W - (cols - 1) * self._gap) / cols))
            cell_h = max(1, int(cell_w / self._aspect))

        rows = math.ceil(n / cols)
        grid_w = cols * cell_w + (cols - 1) * self._gap
        grid_h = rows * cell_h + (rows - 1) * self._gap

        x0 = self._margin + max(0, (W - grid_w) // 2)
        # Centre vertically when everything fits; otherwise anchor to the
        # top so a wrapping QScrollArea scrolls down through the overflow.
        y0 = self._margin + max(0, (H - grid_h) // 2) if grid_h <= H else self._margin

        for i, cell in enumerate(self._cells):
            row, col = divmod(i, cols)
            x = x0 + col * (cell_w + self._gap)
            y = y0 + row * (cell_h + self._gap)
            cell.setGeometry(int(x), int(y), cell_w, cell_h)

        self.setMinimumHeight(max(1, int(grid_h + 2 * self._margin)))
