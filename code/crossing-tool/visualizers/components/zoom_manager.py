"""ZoomManager — small helper owning zoom/view-state for browser pages.

This helper holds the current zoom, applies zoom to items, and handles
persistence via a provided callback. It mirrors the previous inline
behavior in `_MetadataBrowserPage` without changing semantics.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt5.QtCore import QObject, Qt, pyqtSignal


class ZoomManager(QObject):
    """Manage zoom state and notify listeners when it changes.

    Emits `zoomChanged(float)` whenever the zoom value is updated.
    """
    zoomChanged = pyqtSignal(float)
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

        # Notify listeners that the zoom value changed.
        try:
            self.zoomChanged.emit(self._zoom)
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

    # ------------------------------------------------------------ input glue
    def handle_wheel_event(self, event: Any) -> bool:
        """Handle a Ctrl+wheel gesture as a zoom change.

        Mirrors the Ctrl+wheel handling used by Metadata's browser page.
        Returns True (and accepts *event*) if the event was a Ctrl+wheel
        zoom gesture; returns False otherwise so callers can fall back to
        normal event handling.
        """
        if not (event.modifiers() & Qt.ControlModifier):
            return False
        delta = event.angleDelta().y()
        try:
            notches = int(delta / 120)
        except Exception:
            notches = 1 if delta > 0 else (-1 if delta < 0 else 0)
        if notches != 0:
            self.set_zoom(self._zoom + notches * self._step)
        else:
            self.change_zoom(self._step if delta > 0 else -self._step)
        event.accept()
        return True

    def handle_key_event(self, event: Any, default_zoom: float) -> bool:
        """Handle Ctrl+Plus/Minus/0 as zoom keyboard shortcuts.

        Mirrors the keyboard zoom handling used by Metadata's browser page.
        Returns True if *event* was one of the zoom shortcuts; False
        otherwise so callers can fall back to normal key handling.
        """
        mod = event.modifiers()
        if not (mod & Qt.ControlModifier):
            return False
        key = event.key()
        if key in (Qt.Key_Plus, Qt.Key_Equal):
            self.change_zoom(self._step)
            return True
        if key in (Qt.Key_Minus, Qt.Key_Underscore):
            self.change_zoom(-self._step)
            return True
        if key == Qt.Key_0:
            self.set_zoom(default_zoom)
            return True
        return False
