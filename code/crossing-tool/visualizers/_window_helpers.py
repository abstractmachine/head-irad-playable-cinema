from __future__ import annotations

from typing import Iterable

from PyQt5.QtWidgets import QApplication


_VISUALIZER_TITLE = {
    "project":      "Project Visualizer",
    "shotlist":     "Shotlist Visualizer",
    "mosaic":       "Mosaic Visualizer",
    "metadata":     "Metadata Visualizer",
    "cloud":        "Cloud Visualizer",
    "illustration": "Illustration Visualizer",
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


def switch_to_visualizer(subcommand: str) -> None:
    """Raise *subcommand*'s window if it's already open in this process;
    otherwise launch it via the Project Visualizer's existing launcher
    machinery (`ProjectVisualizer._launch`), creating an in-process (not
    shown, unless *subcommand* is ``"project"`` itself) Project window
    first if none exists yet.

    This is the single shared entry point used by F1-F10 visualizer
    switching (see `tool.shortcuts.KeyboardManager`) so it reuses exactly
    the same "raise or launch" behavior as the Project Inspector's
    launcher buttons instead of duplicating that logic.
    """
    if raise_existing_window(subcommand):
        return

    app = QApplication.instance()
    if app is None:
        return

    from visualizers.project_visualizer import ProjectVisualizer

    project_win = next(
        (w for w in app.topLevelWidgets() if isinstance(w, ProjectVisualizer)), None,
    )
    if project_win is None:
        project_win = ProjectVisualizer()
        # Keep a strong reference on the QApplication so PyQt doesn't
        # garbage-collect this hub window while it's only being used
        # internally to launch another visualizer (same pattern already
        # used for `app._keyboard_manager`).
        app._project_window = project_win

    if subcommand == "project":
        project_win.show()
        project_win.raise_()
        project_win.activateWindow()
        return

    project_win._launch(subcommand)
