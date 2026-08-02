"""ScrollbarGutter — shared scrollbar-aware gutter reservation.

Canonical behavior: when a tabbed content area's active tab exposes a
vertical scrollbar (via a `content_scrollbar()` duck-typed hook — e.g.
`TabPanel.content_scrollbar()`) and that scrollbar becomes visible, a
target `GripSplitter` pane is widened by `theme.SCROLLBAR_W` so the
scrollbar has room and neighbouring panes shift left to make space. When
the scrollbar disappears, the width is released and neighbours snap back.

This was originally private to `visualizers.components.inspector.Inspector`
(used by Metadata/Illustration/Project, all of which wrap their tab content
in an `Inspector`). It is extracted here so ANY visualizer that owns a
`TabbedPanel` directly — without wrapping it in an `Inspector` — can opt
into the same canonical behavior. Book Visualizer's control panel is the
first such consumer; a future Shotlist-style "Tab + extra side panel"
layout can reuse it the same way.

A *tab host* is anything exposing:
  - `currentWidget()` -> the active tab's content widget
  - `currentChanged`  -> a Qt signal emitted when the active tab changes

`visualizers.components.tabbed_panel.TabbedPanel` satisfies this
directly, and `Inspector` delegates to this module using its own internal
`TabbedPanel` as the tab host — there is exactly one implementation of
this behavior in the codebase.
"""
from __future__ import annotations

from typing import Dict, Optional

from styles import theme


class ScrollbarGutter:
    """Attaches scrollbar-aware gutter reservation to one or more splitters,
    all driven by the same *tab_host*'s active-tab scrollbar.
    """

    def __init__(self, tab_host) -> None:
        self._tab_host = tab_host
        self._states: Dict[int, dict] = {}
        try:
            tab_host.currentChanged.connect(self._on_tab_changed)
        except Exception:
            pass

    def attach(self, splitter, pane_index: int = 1) -> None:
        """Attach gutter-reservation behavior to *splitter* at *pane_index*.

        When the active tab's vertical scrollbar becomes visible, the
        splitter pane at *pane_index* is widened by `theme.SCROLLBAR_W`;
        when it becomes invisible the width is released. Tracking
        automatically re-binds to whichever tab is active, so a hidden
        tab's scrollbar can never influence the gutter. Multiple calls
        with the same *splitter* are idempotent.
        """
        key = id(splitter)
        if key in self._states:
            return
        state = {
            "splitter": splitter,
            "pane_index": pane_index,
            "visible": False,
            "scrollbar": None,
            "slot": None,
        }
        self._states[key] = state
        self._bind(state)

    def _active_scrollbar(self):
        widget = self._tab_host.currentWidget()
        get_scrollbar = getattr(widget, "content_scrollbar", None)
        if callable(get_scrollbar):
            return get_scrollbar()
        return None

    def _bind(self, state: dict) -> None:
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
            idx = state["pane_index"]
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

    def _on_tab_changed(self, _index: int) -> None:
        for state in self._states.values():
            self._bind(state)


def attach_scrollbar_gutter(tab_host, splitter, pane_index: int = 1) -> Optional[ScrollbarGutter]:
    """Attach (or reuse) a `ScrollbarGutter` for *tab_host* and bind it to
    *splitter* at *pane_index*.

    The `ScrollbarGutter` instance is cached on *tab_host* itself (as
    `_scrollbar_gutter`) so repeated calls for the same tab host share one
    instance across multiple splitters, and calling this twice for the
    same (tab_host, splitter) pair is a no-op (see `ScrollbarGutter.attach`).
    """
    if tab_host is None:
        return None
    gutter = getattr(tab_host, "_scrollbar_gutter", None)
    if gutter is None:
        gutter = ScrollbarGutter(tab_host)
        try:
            tab_host._scrollbar_gutter = gutter
        except Exception:
            pass
    gutter.attach(splitter, pane_index)
    return gutter
