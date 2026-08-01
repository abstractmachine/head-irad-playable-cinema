"""Inspector — thin composition shell hosting a shared `TabbedPanel`.

Responsibilities:
 - Own a single `TabbedPanel` and expose small pass-through helpers
   (`add_tab`, Qt-style `setCurrentIndex`/`currentIndex`) so visualizers
   compose tabs without reaching into `TabbedPanel` directly.
 - Keep scrollbar-gutter reservation (widening/narrowing the splitter pane
   by `theme.SCROLLBAR_W`) attached to the *active* tab's scroll host,
   re-binding automatically whenever the active tab changes. A tab's
   content is expected to be a `TabPanel` (or any widget exposing
   `content_scrollbar()`); tabs without a scroll host simply do not
   participate in gutter reservation.

The Inspector intentionally knows nothing about what a tab's content
contains beyond the optional `content_scrollbar()` duck-typed hook.
"""
from __future__ import annotations

from typing import Dict, Optional

from PyQt5.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from styles import theme
from visualizers.components.tabbed_panel import Tab, TabbedPanel


class Inspector(QWidget):
    """A minimal inspector composition surface built on `TabbedPanel`."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._tabbed = TabbedPanel(self)
        self._tabbed.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._tabbed.currentChanged.connect(self._on_current_tab_changed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._tabbed)

        # id(splitter) -> gutter tracking state; see attach_scrollbar_gutter.
        self._gutter_states: Dict[int, dict] = {}

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
    def _active_scrollbar(self):
        """Return the active tab's vertical scrollbar, if it exposes one."""
        widget = self._tabbed.currentWidget()
        get_scrollbar = getattr(widget, "content_scrollbar", None)
        if callable(get_scrollbar):
            return get_scrollbar()
        return None

    def attach_scrollbar_gutter(self, splitter, inspector_index: int = 1) -> None:
        """Attach gutter-reservation behavior to *splitter*, driven by the
        active tab's scrollbar.

        When the active tab's vertical scrollbar becomes visible, the
        splitter pane at *inspector_index* is widened by
        `theme.SCROLLBAR_W`; when it becomes invisible the width is
        released. Tracking automatically re-binds to whichever tab is
        active, so a hidden tab's scrollbar can never influence the
        gutter. Multiple calls with the same *splitter* are idempotent.
        """
        key = id(splitter)
        if key in self._gutter_states:
            return
        state = {
            "splitter": splitter,
            "inspector_index": inspector_index,
            "visible": False,
            "scrollbar": None,
            "slot": None,
        }
        self._gutter_states[key] = state
        self._bind_gutter(state)

    def _bind_gutter(self, state: dict) -> None:
        old_scrollbar = state["scrollbar"]
        if old_scrollbar is not None and state["slot"] is not None:
            try:
                old_scrollbar.rangeChanged.disconnect(state["slot"])
            except TypeError:
                pass

        scrollbar = self._active_scrollbar()
        state["scrollbar"] = scrollbar
        if scrollbar is None:
            state["slot"] = None
            return

        def _on_range(_min: int, _max: int, state=state) -> None:
            visible = _max > 0
            if state["visible"] == visible:
                return
            splitter = state["splitter"]
            idx = state["inspector_index"]
            sizes = list(splitter.sizes())
            if len(sizes) <= idx:
                state["visible"] = visible
                return
            sb_w = theme.SCROLLBAR_W
            sizes[idx] = sizes[idx] + sb_w if visible else max(0, sizes[idx] - sb_w)
            state["visible"] = visible
            splitter.setSizes(sizes)

        state["slot"] = _on_range
        scrollbar.rangeChanged.connect(_on_range)
        _on_range(scrollbar.minimum(), scrollbar.maximum())

    def _on_current_tab_changed(self, _index: int) -> None:
        for state in self._gutter_states.values():
            self._bind_gutter(state)

