"""ThumbnailLoader — shared background thumbnail loading worker.

Loads thumbnail images off the GUI thread and delivers QImages via Qt signals,
keeping the browser responsive during large scans.

This is the canonical implementation that replaces the three separate loader
classes currently found in illustration_visualizer.py and book_visualizer.py:

  - ``_ThumbLoader``        (illustration_visualizer.py)
  - ``_BrowserThumbLoader`` (book_visualizer.py)
  - ``_EngravingThumbLoader`` (book_visualizer.py)

Migration of existing callers to this canonical class is deferred to Phase 2.

Usage::

    records = [{"path": Path("object_0001.json")}, ...]

    loader = ThumbnailLoader(records, size=120)
    loader.thumbReady.connect(self._on_thumb_ready)
    loader.loadFinished.connect(self._on_load_finished)
    loader.start()

    # In GUI thread:
    def _on_thumb_ready(self, index: int, qimg: QImage) -> None:
        self._cells[index].set_image(qimg)

    def _on_load_finished(self, loaded: int) -> None:
        self._status_lbl.setText(f"{loaded} thumbnails loaded")

    # To cancel before completion:
    loader.cancel()

Notes
-----
- ``QImage`` is safe to construct off the GUI thread; ``QPixmap`` conversion
  must happen in the GUI thread (in the ``thumbReady`` slot).
- ``path_for(record)`` resolves the PNG path for each record dict.  The
  default resolver uses ``record["path"].with_suffix(".png")``, which matches
  the silhouette catalog layout.  Pass a custom callable to support other
  layouts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QImage

# Number of items loaded before the thread yields the event loop briefly so
# the GUI remains responsive during large batch loads.
_YIELD_EVERY = 20


def _default_path_for(record: dict) -> Optional[Path]:
    """Return the PNG path for a silhouette catalog record.

    Resolves ``record["path"]`` (a JSON sidecar Path) to the sibling PNG.
    Returns ``None`` when the key is absent or the file does not exist.
    """
    json_path = record.get("path")
    if not json_path:
        return None
    png = Path(str(json_path)).with_suffix(".png")
    return png if png.exists() else None


class ThumbnailLoader(QThread):
    """Background thumbnail loader for browser grids.

    Signals
    -------
    thumbReady(index, qimage)
        Emitted for each successfully loaded thumbnail.  ``index`` is the
        position of the record in the input list.  ``qimage`` is a copy safe
        to use from the GUI thread.
    loadFinished(loaded_count)
        Emitted once when the run loop exits (either after processing all
        records or after ``cancel()`` is called).  ``loaded_count`` is the
        number of thumbnails that were successfully loaded.

    Parameters
    ----------
    records:
        List of item dicts to process.  The exact schema depends on the data
        source; the default ``path_for`` resolver handles silhouette catalog
        records.
    size:
        Thumbnail bounding box in pixels (images are scaled to fit within
        ``size × size`` while preserving aspect ratio).
    path_for:
        Optional callable ``(record: dict) -> Path | None`` that resolves each
        record to its PNG path.  Defaults to ``_default_path_for`` which reads
        ``record["path"].with_suffix(".png")``.
    parent:
        Optional parent QObject.

    Ownership
    ---------
    Path resolution belongs to the source (or ``path_for`` callback).
    This loader owns threaded image decoding only.
    """

    thumbReady   = pyqtSignal(int, QImage)   # (index, qimage)
    loadFinished = pyqtSignal(int)           # (loaded_count)

    def __init__(
        self,
        records: list[dict],
        size: int,
        path_for: Optional[Callable[[dict], Optional[Path]]] = None,
        timing_start: float | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._records  = records
        self._size     = size
        self._path_for = path_for or _default_path_for
        self._cancelled = False

    def cancel(self) -> None:
        """Signal the loader to stop after the current item.  Non-blocking."""
        self._cancelled = True

    def run(self) -> None:
        try:
            from PIL import Image as _PIL
        except ImportError:
            self.loadFinished.emit(0)
            return

        loaded = 0
        for i, record in enumerate(self._records):
            if self._cancelled:
                break

            png_path = self._path_for(record)
            if png_path is None:
                continue

            try:
                img = _PIL.open(str(png_path)).convert("RGBA")
                img.thumbnail((self._size, self._size), _PIL.LANCZOS)
                w, h = img.size
                data = img.tobytes("raw", "RGBA")
                qimg = QImage(data, w, h, 4 * w, QImage.Format_RGBA8888)
                self.thumbReady.emit(i, qimg.copy())
                loaded += 1
            except Exception:
                pass

            if (i + 1) % _YIELD_EVERY == 0:
                self.msleep(2)   # yield so GUI stays responsive

        self.loadFinished.emit(loaded)
