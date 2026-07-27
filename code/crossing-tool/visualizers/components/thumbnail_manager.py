"""ThumbnailManager — small owner for progressive thumbnail loading.

This helper centralizes the lifecycle and signal wiring for
``ThumbnailLoader`` without embedding any metadata-specific logic.

It receives the records list, a `path_for(record)` resolver, and an
`apply_callback(index, QImage)` that it will call on every delivered
thumbnail. The manager owns creating / stopping the loader and forwards
thumbReady events to the provided callback.
"""

from __future__ import annotations

from typing import Callable, Optional, Any

from PyQt5.QtCore import QObject
from PyQt5.QtGui import QImage

from visualizers.components.thumbnail_loader import ThumbnailLoader


class ThumbnailManager(QObject):
    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._parent = parent
        self._loader: Optional[ThumbnailLoader] = None
        self._apply_cb: Optional[Callable[[int, QImage], None]] = None

    def start(
        self,
        records: list[dict],
        size: int,
        path_for: Callable[[dict], Optional[Any]],
        apply_callback: Callable[[int, QImage], None],
    ) -> None:
        """Start a new ThumbnailLoader for *records*.

        If a previous loader is active it will be stopped first.
        """
        # Stop any existing loader
        self.stop()

        if not records:
            return

        self._apply_cb = apply_callback
        self._loader = ThumbnailLoader(records, size, path_for=path_for, parent=self._parent)
        self._loader.thumbReady.connect(self._on_thumb_ready)
        self._loader.start()

    def stop(self) -> None:
        """Stop and release the current loader (if any)."""
        if self._loader is None:
            return
        try:
            self._loader.thumbReady.disconnect(self._on_thumb_ready)
        except Exception:
            pass
        try:
            if self._loader.isRunning():
                self._loader.cancel()
                self._loader.wait(300)
        except Exception:
            pass
        self._loader = None
        self._apply_cb = None

    def _on_thumb_ready(self, index: int, qimg: QImage) -> None:
        """Internal slot: forward the delivered QImage to the callback."""
        try:
            if self._apply_cb is not None:
                self._apply_cb(index, qimg)
        except Exception:
            # Ensure loader continues even if the callback misbehaves
            pass
