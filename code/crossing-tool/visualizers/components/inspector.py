"""Inspector — thin composition shell hosting a shared `TabbedPanel`.

Responsibilities:
 - Own a single `TabbedPanel` and expose small pass-through helpers
   (`add_tab`, Qt-style `setCurrentIndex`/`currentIndex`) so visualizers
   compose tabs without reaching into `TabbedPanel` directly.
 - Participate in scrollbar-gutter reservation (widening/narrowing a
   splitter pane by `theme.SCROLLBAR_W` when a tab's content scrollbar
   appears/disappears) via the shared `scrollbar_gutter` module, using its
   own internal `TabbedPanel` as the "tab host". A tab's content is
   expected to be a `TabPanel` (or any widget exposing
   `content_scrollbar()`); tabs without a scroll host simply do not
   participate in gutter reservation.

The Inspector intentionally knows nothing about what a tab's content
contains beyond the optional `content_scrollbar()` duck-typed hook. The
gutter-reservation mechanism itself lives in
`visualizers.components.scrollbar_gutter` so any visualizer that owns a
`TabbedPanel` directly (not wrapped in an `Inspector`) can reuse the exact
same behavior — see `WindowVisualizer`'s `gutter_tab_host` duck-type hook.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from visualizers.components.scrollbar_gutter import attach_scrollbar_gutter
from visualizers.components.tabbed_panel import Tab, TabbedPanel


class Inspector(QWidget):
    """A minimal inspector composition surface built on `TabbedPanel`."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._tabbed = TabbedPanel(self)
        self._tabbed.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._tabbed)

        # Duck-typed hook: WindowVisualizer looks for this attribute on
        # whatever create_inspector()/create_side_panel() returns to attach
        # shared scrollbar-gutter behavior (see scrollbar_gutter.py). Any
        # widget exposing a `TabbedPanel`-shaped tab host this way
        # participates, not just Inspector instances.
        self.gutter_tab_host = self._tabbed

    # ------------------------------------------------------------ tab building
    def add_tab(self, content: QWidget, title: str) -> Tab:
        """Add *content* (typically a `TabPanel`) as a new tab; return its `Tab`."""
        return self._tabbed.add_tab(content, title)

    def tabbed_panel(self) -> TabbedPanel:
        """Return the underlying `TabbedPanel` for rare direct access."""
        return self._tabbed

    def set_minimum_width(self, width: int) -> None:
        """Set the minimum width of the inspector's tabbed panel."""
        self._tabbed.setMinimumWidth(int(width))

    # ------------------------------------------------------------ Qt-style API
    def setCurrentIndex(self, index: int) -> None:
        self._tabbed.setCurrentIndex(index)

    def currentIndex(self) -> int:
        return self._tabbed.currentIndex()

    # ------------------------------------------------------------ scrollbar gutter
    def attach_scrollbar_gutter(self, splitter, inspector_index: int = 1) -> None:
        """Attach gutter-reservation behavior to *splitter*, driven by the
        active tab's scrollbar. Thin convenience wrapper around the shared
        `scrollbar_gutter.attach_scrollbar_gutter()` using this Inspector's
        own `TabbedPanel` as the tab host — see that module for the full
        behavior contract.
        """
        attach_scrollbar_gutter(self._tabbed, splitter, inspector_index)

