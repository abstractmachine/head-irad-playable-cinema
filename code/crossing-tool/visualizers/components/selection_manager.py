"""SelectionManager — small helper to own selection state for browser pages.

This helper centralizes selection index ownership and the side-effects
of changing selection (clearing previous selection, updating visuals,
scroll-into-view, and emitting activation/select signals).

It intentionally does not change the public signals or the surrounding
browser behavior; callers should delegate selection-related methods to
this manager.
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import QObject


class SelectionManager(QObject):
    def __init__(self, page: Any) -> None:
        super().__init__(page)
        self._page = page
        self._selected_index: int = getattr(page, "_selected_index", -1)
        try:
            self._page._selected_index = self._selected_index
        except Exception:
            pass

    def set_selected_index(self, index: int, emit: bool = True) -> None:
        """Set the selected index, updating visuals and emitting selection."""
        records = getattr(self._page, "_records", None)
        if not records:
            self._selected_index = -1
            try:
                self._page._selected_index = -1
            except Exception:
                pass
            if emit:
                try:
                    self._page.selectionChanged.emit(None)
                except Exception:
                    pass
            return

        # Clamp into valid range for the record list
        idx = max(0, min(index, len(records) - 1))
        if idx == self._selected_index and emit:
            self.emit_current_selection()
            return

        items = getattr(self._page, "_item_by_index", [])

        # Clear previous selection visual
        if 0 <= self._selected_index < len(items):
            try:
                items[self._selected_index].set_selected(False)
            except Exception:
                pass

        # Update owned index and mirror onto the page for compatibility
        self._selected_index = idx
        try:
            self._page._selected_index = idx
        except Exception:
            pass

        # Apply new selection visual and ensure it's visible
        if 0 <= self._selected_index < len(items):
            try:
                items[self._selected_index].set_selected(True)
                try:
                    # Keep original behavior: use page's scroll to ensure
                    # the selected widget is visible.
                    self._page._scroll.ensureWidgetVisible(items[self._selected_index])
                except Exception:
                    pass
            except Exception:
                pass

        if emit:
            self.emit_current_selection()

    def emit_current_selection(self) -> None:
        try:
            self._page.selectionChanged.emit(self._page.current_record())
        except Exception:
            pass

    def on_cell_clicked(self, index: int) -> None:
        try:
            self._page.setFocus()
        except Exception:
            pass
        self.set_selected_index(index, emit=True)

    def on_cell_double_clicked(self, index: int) -> None:
        self.on_cell_clicked(index)
        try:
            self._page.openRequested.emit(self._page.current_record())
        except Exception:
            pass
