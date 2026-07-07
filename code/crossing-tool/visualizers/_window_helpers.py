from __future__ import annotations

from typing import Iterable

from PyQt5.QtWidgets import QApplication


_VISUALIZER_TITLE = {
    "shotlist":     "Shotlist Visualizer",
    "mosaic":       "Mosaic Visualizer",
    "composition":  "Composition Visualizer",
    "metadata":     "Metadata Visualizer",
    "cloud":        "Cloud Visualizer",
    "silhouette":   "Silhouette Visualizer",
    "palette":      "Palette Visualizer",
    "flipbook":     "Flipbook Visualizer",
    "book":         "Book Visualizer",
    "sync":         "Sync Visualizer",
    "segmentation": "Segmentation Visualizer",
}


def _candidate_titles(subcommand: str) -> set[str]:
    title = _VISUALIZER_TITLE.get(subcommand, subcommand.capitalize())
    short = title.replace(" Visualizer", "")
    caps = subcommand.capitalize()
    return {title, short, caps, f"Crossing — {short}", f"Crossing — {title}", f"Crossing — {caps}"}


def raise_existing_window(subcommand: str) -> bool:
    """If a top-level widget matching *subcommand* exists in this process,
    bring it to the front and return True. Otherwise return False.

    This works reliably when the target window was opened in-process (same
    QApplication).  For cross-process windows use OS-level tools instead.
    """
    try:
        app = QApplication.instance()
    except Exception:
        app = None
    if app is None:
        return False

    from PyQt5.QtCore import Qt, QTimer

    candidates = _candidate_titles(subcommand)
    lower_candidates = {c.lower() for c in candidates if c}

    for w in app.topLevelWidgets():
        try:
            wtitle = w.windowTitle() or ""
        except Exception:
            continue
        lt = wtitle.lower()
        if not any((c == lt or c in lt) for c in lower_candidates):
            continue

        # Un-minimise, restore, and activate the window.
        try:
            w.show()
            w.showNormal()
            w.setWindowState((w.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
            w.raise_()
            w.activateWindow()
        except Exception:
            pass

        # Schedule a second activation on the next event-loop tick —
        # some window managers defer focus grants until after paint.
        try:
            def _retry(ref=w):
                try:
                    ref.raise_()
                    ref.activateWindow()
                except Exception:
                    pass
            QTimer.singleShot(50, _retry)
        except Exception:
            pass

        return True

    return False
