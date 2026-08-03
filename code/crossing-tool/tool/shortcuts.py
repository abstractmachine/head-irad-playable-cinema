"""Central keyboard shortcut definitions and base window class for all visualizers.

Import the named constants instead of raw ``Qt.Key_*`` values so that any
future re-mapping only needs to change this file.

``VisualizerWindow`` is the shared QMainWindow base every visualizer window
should inherit from.  ``KeyboardManager`` is the single application-level
event filter that centralizes keyboard behavior common to every visualizer:

- Escape: close the active visualizer
- Tab / Shift+Tab: canonical panel toggle / fullscreen (intercepted before
  Qt's focus-cycling machinery claims them, then forwarded to the active
  window's ``keyPressEvent``, which implements the actual behavior)
- Shift+Left / Shift+Right: page navigation (also forwarded)
- F1-F10, F12: switch to another visualizer (see `FUNCTION_KEY_BINDINGS`
  below), regardless of which visualizer window currently has focus

It is installed once per QApplication by ``VisualizerWindow.__init__``.
This is intentionally the ONE shared/global keyboard mechanism — it must
stay small and must never grow into a second, competing shortcut system.
Visualizer-specific keys (arrow-key navigation, tool toggles, etc.) still
belong in each visualizer's own ``keyPressEvent``/``eventFilter``.
"""

from typing import Optional

from PyQt5.QtCore import Qt, QEvent, QObject
from PyQt5.QtWidgets import QApplication, QMainWindow


# ── Function-key visualizer switching ────────────────────────────────────
# The ONE place the F1-F10/F12 -> visualizer mapping is defined. Both
# `KeyboardManager` (global key interception, below) and the Project
# Inspector's on-button shortcut labels (`shortcut_label_for`) derive from
# this list, so the two can never drift out of sync. Edit this list to
# change the mapping — nothing else needs to change.
FUNCTION_KEY_BINDINGS: list[tuple[int, str]] = [
    (Qt.Key_F1,  "project"),
    (Qt.Key_F2,  "metadata"),
    (Qt.Key_F3,  "shotlist"),
    (Qt.Key_F4,  "mosaic"),
    (Qt.Key_F5,  "cloud"),
    (Qt.Key_F6,  "segmentation"),
    (Qt.Key_F7,  "illustration"),
    (Qt.Key_F8,  "flipbook"),
    (Qt.Key_F9,  "palette"),
    (Qt.Key_F10, "book"),
    (Qt.Key_F12, "sync"),
]

# Key (int) -> subcommand, used by KeyboardManager for dispatch.
FUNCTION_KEY_VISUALIZERS: dict = dict(FUNCTION_KEY_BINDINGS)

# Qt key constant -> display name, used below to build
# _VISUALIZER_SHORTCUT_LABELS from the actual key rather than by
# enumeration position, since F11 is intentionally unused (Sync is
# bound to F12, not the next sequential key).
_FUNCTION_KEY_NAMES: dict = {
    Qt.Key_F1: "F1", Qt.Key_F2: "F2", Qt.Key_F3: "F3", Qt.Key_F4: "F4",
    Qt.Key_F5: "F5", Qt.Key_F6: "F6", Qt.Key_F7: "F7", Qt.Key_F8: "F8",
    Qt.Key_F9: "F9", Qt.Key_F10: "F10", Qt.Key_F11: "F11", Qt.Key_F12: "F12",
}

# subcommand -> "F1".."F12", used by UI code (e.g. Project Inspector
# buttons) to display the binding without hardcoding it a second time.
_VISUALIZER_SHORTCUT_LABELS: dict = {
    sub: _FUNCTION_KEY_NAMES[key] for key, sub in FUNCTION_KEY_BINDINGS
}


def shortcut_label_for(subcommand: str) -> Optional[str]:
    """Return the canonical function-key label (e.g. ``"F9"``) bound to
    *subcommand* in `FUNCTION_KEY_BINDINGS`, or None if it has no binding.
    """
    return _VISUALIZER_SHORTCUT_LABELS.get(subcommand)


class KeyboardManager(QObject):
    """Application-level event filter that centralizes keyboard behavior
    common to every visualizer window.

    Keys intercepted (with no Ctrl/Meta/Alt):
    - Escape: close the active visualizer immediately
    - Tab / Shift+Tab: panel toggle / fullscreen (forwarded to the active
      VisualizerWindow's own keyPressEvent, which implements the actual
      behavior)
    - Shift+Left / Shift+Right: page navigation (also forwarded)
    - F1-F10, F12: switch to the visualizer bound in `FUNCTION_KEY_BINDINGS`,
      via `visualizers._window_helpers.switch_to_visualizer` — works
      regardless of which window currently has focus, since this filter
      is installed on the QApplication itself.

    Installed once on the QApplication instance (stored as
    ``app._keyboard_manager``) so multiple windows share a single filter.
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.KeyPress:
            key = event.key()
            mod = event.modifiers()
            if mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier):
                return False
            if key in FUNCTION_KEY_VISUALIZERS:
                # Local import: tool/ must not import visualizers/ at module
                # scope (layering — visualizers/ already imports tool/).
                from visualizers._window_helpers import switch_to_visualizer
                switch_to_visualizer(FUNCTION_KEY_VISUALIZERS[key])
                return True
            if key == Qt.Key_Escape:
                app = QApplication.instance()
                win = app.activeWindow() if app else None
                if win is not None and hasattr(win, "close"):
                    win.close()
                    return True
                return False
            intercept = (
                key in (Qt.Key_Tab, Qt.Key_Backtab)
                or (key in (Qt.Key_Left, Qt.Key_Right) and bool(mod & Qt.ShiftModifier))
            )
            if intercept:
                app = QApplication.instance()
                win = app.activeWindow() if app else None
                if isinstance(win, VisualizerWindow):
                    win.keyPressEvent(event)
                    return True
        return False


def install_keyboard_manager(app) -> None:
    """Ensure `KeyboardManager` is installed on *app*.

    Idempotent (a no-op after the first call for a given *app*), so it's
    safe to call from every place that might construct the first top-level
    crossing-tool window in a process: `VisualizerWindow.__init__` (below),
    the shared standalone-launch helpers in `visualizers.launcher`, and any
    other bespoke visualizer bootstrap (e.g. Shotlist's, which predates
    this shared mechanism and builds its own `QApplication` directly).
    Without calling this from *every* such entry point, F1-F10/F12/Tab/
    Shift+Tab would silently do nothing whenever the first window opened in
    a fresh process happens to be one that doesn't itself subclass
    `VisualizerWindow` (Shotlist/Sync are still plain QMainWindow).
    """
    if app is not None and not hasattr(app, '_keyboard_manager'):
        app._keyboard_manager = KeyboardManager(app)
        app.installEventFilter(app._keyboard_manager)


class VisualizerWindow(QMainWindow):
    """Base window class for all crossing-tool visualizers.

    Installs a single application-level event filter (KeyboardManager) the
    first time any VisualizerWindow is constructed.  This filter intercepts
    Tab / Shift+Tab / F1-F10 / F12 before Qt routes them as focus-change events
    (or before any window-specific handling could see them), forwarding
    Tab/Shift+Tab to the active window's keyPressEvent.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        install_keyboard_manager(QApplication.instance())

# ── Title-list navigation (movies or gameplay, consistent across all visualizers) ──
KEY_PREV_TITLE = Qt.Key_Home
KEY_NEXT_TITLE = Qt.Key_End

# ── Item navigation (context-dependent: scene in shot visualizer, etc.) ──────
KEY_PREV_ITEM = Qt.Key_PageUp
KEY_NEXT_ITEM = Qt.Key_PageDown

# ── Shot navigation (within current scene / film) ─────────────────────────────
KEY_PREV_SHOT = Qt.Key_Up
KEY_NEXT_SHOT = Qt.Key_Down

# ── Frame-level playback stepping ─────────────────────────────────────────────
KEY_PREV_FRAME = Qt.Key_Left
KEY_NEXT_FRAME = Qt.Key_Right
KEY_PLAY_PAUSE = Qt.Key_Space
