"""Inspector — simple reusable inspector composition surface.

Responsibilities:
 - Own a QScrollArea and a vertical layout for composing CollapsibleSections
 - Provide consistent spacing and a small public API: add_group() and clear()

The Inspector intentionally knows nothing about the content it hosts.
"""
from __future__ import annotations

from typing import List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from styles import theme
from visualizers.components.tab_panel import TabPanel


class Inspector(QWidget):
    """A minimal inspector composition surface.

    Public API:
      - add_group(title: str, widget: QWidget) -> CollapsibleSection
      - clear()
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # The TabPanel is the single canonical content widget for the inspector.
        # The TabPanel owns a fixed header and an internal QScrollArea for the
        # content region. Keep a reference to that internal scroll area here
        # for compatibility with existing callers.
        self._panel = TabPanel(self)
        self._panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Expose the internal content scroll area for backwards compatibility
        # so code that listens to scrollbar signals can connect to it.
        try:
            self.scroll = self._panel._content_scroll
        except Exception:
            self.scroll = None

        # Layout for this Inspector widget — host the TabPanel directly.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._panel)

    def panel(self) -> TabPanel:
        """Return the TabPanel owned by this Inspector."""
        return self._panel

    def set_minimum_width(self, width: int) -> None:
        """Set the minimum width of the inspector panel widget."""
        try:
            self._panel.setMinimumWidth(int(width))
        except Exception:
            pass

    def connect_scrollbar_range_changed(self, slot) -> None:
        """Connect *slot(min, max)* to the internal vertical scrollbar's
        `rangeChanged` signal without exposing the scrollbar widget.
        """
        try:
            self.scroll.verticalScrollBar().rangeChanged.connect(slot)
        except Exception:
            pass

