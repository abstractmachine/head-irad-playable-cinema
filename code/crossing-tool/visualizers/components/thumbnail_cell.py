"""ThumbnailCell — single thumbnail cell widget.

Canonical thumbnail cell for the Visualizer Framework.  Combines the
features of the two existing implementations:

- ``_ThumbnailCell``    (silhouette_visualizer.py) — single click, best state
- ``_BrowserThumbCell`` (book_visualizer.py)       — double click, drag-and-drop

Migration of existing callers to this canonical class is deferred to Phase 2.

Features
--------
- Configurable fixed size
- ``clicked(index)`` signal on single left-click
- ``doubleClicked(index)`` signal on double left-click
- ``set_selected(bool)`` — fuchsia border around the cell
- ``set_highlighted(bool)`` — thicker fuchsia border (marks the "best" item)
- ``set_image(QImage)`` — update the displayed thumbnail
- Drag-and-drop: set ``drag_path`` and ``drag_meta`` before the user drags

Usage::

    cell = ThumbnailCell(index=0, size=120, tooltip="horse — Django (1966)")
    cell.clicked.connect(self._on_click)
    cell.doubleClicked.connect(self._on_double_click)
    loader.thumbReady.connect(lambda i, img: cell.set_image(img) if i == 0 else None)

    # Enable drag-and-drop:
    cell.drag_path = "/path/to/object_0001.png"
    cell.drag_meta = {"label": "horse", "film": "django_1966"}
"""

from __future__ import annotations

import json
from typing import Optional

from PyQt5.QtCore import QByteArray, QMimeData, QPoint, Qt, pyqtSignal
from PyQt5.QtGui import QDrag, QImage, QPixmap
from PyQt5.QtWidgets import QApplication, QLabel, QWidget

from styles import theme


class ThumbnailCell(QLabel):
    """Single thumbnail cell — fixed size, selectable, optionally draggable.

    Parameters
    ----------
    index:
        Position of this cell in its parent grid.  Emitted with all signals
        so the parent can identify which cell was interacted with.
    size:
        Fixed cell dimension in pixels (square).  Defaults to 120.
    tooltip:
        Optional tooltip string.
    parent:
        Optional parent widget.
    """

    clicked       = pyqtSignal(int)   # index
    doubleClicked = pyqtSignal(int)   # index

    def __init__(
        self,
        index: int,
        size: int = 120,
        tooltip: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._index        = index
        self._selected     = False
        self._highlighted  = False   # "best" state — thicker accent border
        self._light_bg     = False   # engraving mode: white/grey backgrounds
        self._press_pos: Optional[QPoint] = None

        # Drag-and-drop — set these before the user can drag
        self.drag_path: str  = ""    # absolute path to the draggable PNG
        self.drag_meta: dict = {}    # metadata dict included in the drag payload

        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
        self.setText("·")   # placeholder until image loads
        self._apply_style()

    # ------------------------------------------------------------------ public

    def set_image(self, qimg: QImage) -> None:
        """Set the displayed thumbnail from a QImage (GUI thread only)."""
        self.setPixmap(QPixmap.fromImage(qimg))
        self.setText("")

    def set_selected(self, selected: bool) -> None:
        """Toggle the selection border (fuchsia, 2 px)."""
        if self._selected != selected:
            self._selected = selected
            self._apply_style()

    def set_highlighted(self, highlighted: bool) -> None:
        """Toggle the highlight border (fuchsia, 3 px — marks the best item)."""
        if self._highlighted != highlighted:
            self._highlighted = highlighted
            self._apply_style()

    def set_light_bg(self, enabled: bool) -> None:
        """Use light (white/grey) backgrounds for transparent-image grids.

        When *enabled* is ``True``:
        - default state        — light grey background
        - selected (not best)  — white background
        - selected + best      — accent colour (same as dark mode)
        - best (not selected)  — light grey with accent border
        """
        if self._light_bg != enabled:
            self._light_bg = enabled
            self._apply_style()

    # ------------------------------------------------------------------ events

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
            self.clicked.emit(self._index)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.doubleClicked.emit(self._index)
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (
            event.buttons() & Qt.LeftButton
            and self._press_pos is not None
            and self.drag_path
            and (event.pos() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()
        ):
            mime = QMimeData()
            payload = json.dumps(
                {"abs_path": self.drag_path, "meta": self.drag_meta}
            )
            mime.setData(
                "application/x-crossing-illus-source",
                QByteArray(payload.encode()),
            )
            drag = QDrag(self)
            drag.setMimeData(mime)
            pix = self.pixmap()
            if pix and not pix.isNull():
                scaled = pix.scaled(
                    48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                drag.setPixmap(scaled)
                drag.setHotSpot(
                    QPoint(scaled.width() // 2, scaled.height() // 2)
                )
            self._press_pos = None   # prevent re-triggering on same press
            drag.exec_(Qt.CopyAction)
            return
        super().mouseMoveEvent(event)

    # ------------------------------------------------------------------ private

    def _apply_style(self) -> None:
        # selected + best  → grey outer border, ACCENT inner ring (via bg fill)
        # best only        → ACCENT border with gap
        # selected only    → grey border with gap
        # neither          → no visible border, no padding
        #
        # light_bg mode (engravings grid): white/grey backgrounds so
        # transparent PNGs are legible against a light surface.
        if self._light_bg:
            if self._highlighted and self._selected:
                border  = "2px solid #808080"
                bg      = theme.ACCENT
                padding = "4px"
            elif self._highlighted:
                border  = f"2px solid {theme.ACCENT}"
                bg      = "#808080"
                padding = "2px"
            elif self._selected:
                border  = "2px solid #a0a0a0"
                bg      = "#ffffff"
                padding = "4px"
            else:
                border  = "1px solid #808080"
                bg      = "#808080"
                padding = "0px"
        else:
            if self._highlighted and self._selected:
                border  = "2px solid #808080"
                bg      = theme.ACCENT
                padding = "4px"
            elif self._highlighted:
                border  = f"2px solid {theme.ACCENT}"
                bg      = theme.CANVAS_BG
                padding = "2px"
            elif self._selected:
                border  = "2px solid #808080"
                bg      = theme.CANVAS_BG
                padding = "4px"
            else:
                border  = "1px solid transparent"
                bg      = theme.CANVAS_BG
                padding = "0px"

        self.setStyleSheet(
            f"background: {bg}; border: {border}; padding: {padding};"
            f" color: {theme.TEXT_DIM};"
            f" font-family: '{theme.FAMILY_MONO}';"
            f" font-size: {theme.BASE_PT}pt;"
        )
