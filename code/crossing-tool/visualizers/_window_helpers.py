from __future__ import annotations

import re
from typing import Iterable

from PyQt5.QtWidgets import QApplication


_VISUALIZER_TITLE = {
    "project":      "Crossing — Project",
    "shotlist":     "Shotlist",
    "mosaic":       "Mosaic",
    "metadata":     "Metadata",
    "cloud":        "Cloud",
    "illustration": "Illustration",
    "palette":      "Palette",
    "flipbook":     "Flipbook",
    "book":         "Book",
    "sync":         "Sync",
    "segmentation": "Segmentation",
}


def _candidate_titles(subcommand: str) -> set[str]:
    title = _VISUALIZER_TITLE.get(subcommand, subcommand.capitalize())
    caps = subcommand.capitalize()
    return {title, caps}


def _title_matches(candidate: str, title: str) -> bool:
    """True if *candidate* (already lowercased) appears in *title* (already
    lowercased) as a whole word/phrase, not embedded inside a longer word.

    A plain substring check (``candidate in title``) is too loose: "book"
    is a substring of "flipbook", so `raise_existing_window("book")` would
    incorrectly match an already-open Flipbook window instead of Book.
    Word-boundary matching still allows titles with dynamic suffixes (e.g.
    Shotlist's "shotlist — clip.mp4  (1/5)") to match on their
    canonical-name prefix.
    """
    if candidate == title:
        return True
    pattern = r'(?<![a-z0-9])' + re.escape(candidate) + r'(?![a-z0-9])'
    return re.search(pattern, title) is not None


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
        if not any(_title_matches(c, lt) for c in lower_candidates):
            continue

        # Un-minimise, restore, and activate the window. Preserve fullscreen
        # state if the window was already fullscreen — calling showNormal()
        # unconditionally would otherwise kick it back to windowed mode even
        # when the user had intentionally left it fullscreen (e.g. Book was
        # fullscreen, user switched to Illustration via F7, then back to
        # Book via F10 — Book must still be fullscreen, not reset).
        #
        # When already fullscreen, go straight to showFullScreen() instead
        # of calling show() first — showFullScreen() un-minimises AND
        # (re)asserts the fullscreen window state in one call, whereas
        # show()-then-showFullScreen() lets some window managers see (and
        # briefly render, or even keep) the intermediate "shown normally"
        # state as merely maximized instead of truly fullscreen.
        try:
            was_fullscreen = w.isFullScreen()
            if was_fullscreen:
                w.showFullScreen()
            else:
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

    This is the single shared entry point used by F1-F10/F12 visualizer
    switching (see `tool.shortcuts.KeyboardManager`) so it reuses exactly
    the same "raise or launch" behavior as the Project Inspector's
    launcher buttons instead of duplicating that logic.
    """
    # "project" is handled entirely below: the generic `raise_existing_window`
    # scan matches on window title alone, which would also match a
    # never-shown ProjectVisualizer "hub" bootstrapped only to launch some
    # *other* subcommand (see below) — incorrectly treating it as an
    # already-open Project window instead of raising/pinging whichever
    # process (this one or another) actually has Project legitimately open.
    if subcommand != "project" and raise_existing_window(subcommand):
        return

    app = QApplication.instance()
    if app is None:
        return

    from visualizers.project_visualizer import ProjectVisualizer

    project_win = next(
        (w for w in app.topLevelWidgets() if isinstance(w, ProjectVisualizer)), None,
    )

    if subcommand == "project":
        # A ProjectVisualizer that has genuinely been shown as "Project" at
        # least once (`_shown_as_project`, set from `showEvent`) counts as
        # already open — reuse the normal in-process raise (which also
        # preserves fullscreen state). A hidden hub bootstrapped only to
        # launch some other visualizer does not count.
        if project_win is not None and project_win._shown_as_project:
            raise_existing_window("project")
            return

        # Cross-process single-instance guard: another OS process may
        # already have a visible Project window open; if so, ping it to
        # raise itself instead of showing the (possibly just-bootstrapped,
        # hidden) one in this process.
        from tool import prefs as _prefs
        from visualizers.components.singleton_guard import claim_or_ping_and_bind
        if not claim_or_ping_and_bind("project", _prefs.get("path") or "", app):
            return

        if project_win is None:
            project_win = ProjectVisualizer()
            # Keep a strong reference on the QApplication so PyQt doesn't
            # garbage-collect this hub window while it's only being used
            # internally to launch another visualizer (same pattern already
            # used for `app._keyboard_manager`).
            app._project_window = project_win

        project_win.show()
        project_win.raise_()
        project_win.activateWindow()
        return

    if project_win is None:
        project_win = ProjectVisualizer()
        app._project_window = project_win

    project_win._launch(subcommand)
