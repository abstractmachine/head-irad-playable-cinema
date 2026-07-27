"""ZoomManager — small helper owning zoom/view-state for browser pages.

This helper holds the current zoom, applies zoom to items, and handles
persistence via a provided callback. It mirrors the previous inline
behavior in `_MetadataBrowserPage` without changing semantics.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt5.QtCore import QObject


class ZoomManager(QObject):
    def __init__(
        self,
        page: Any,
        initial_zoom: float,
        min_zoom: float,
        max_zoom: float,
        step: float,
        persist_cb: Optional[Callable[[float], None]] = None,
    ) -> None:
        super().__init__(page)
        self._page = page
        self._min = min_zoom
        self._max = max_zoom
        self._step = step
        self._persist_cb = persist_cb
        self._zoom = float(initial_zoom)
        # Keep the page attribute in sync for compatibility
        try:
            self._page._zoom = self._zoom
        except Exception:
            pass

    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float, persist: bool = True) -> None:
        z = max(self._min, min(self._max, zoom))
        if abs(z - self._zoom) < 1e-6:
            return
        self._zoom = z
        if persist and self._persist_cb is not None:
            try:
                self._persist_cb(self._zoom)
            except Exception:
                pass
        try:
            self._page._zoom = self._zoom
        except Exception:
            pass

        # Apply zoom to items
        try:
            for item in getattr(self._page, "_item_by_index", []):
                item.set_zoom(self._zoom)
        except Exception:
            pass

        # Try an immediate reflow; fall back to debounced request_reflow()
        try:
            self._page._grid_widget._do_flow_layout()
            self._page._grid_cols = max(1, self._page._grid_widget.first_row_count())
        except Exception:
            try:
                self._page.request_reflow()
            except Exception:
                pass

    def change_zoom(self, delta: float) -> None:
        self.set_zoom(self._zoom + delta)

    def apply_zoom_to_items(self) -> None:
        try:
            for item in getattr(self._page, "_item_by_index", []):
                item.set_zoom(self._zoom)
        except Exception:
            pass
        try:
            self._page.request_reflow()
        except Exception:
            pass
