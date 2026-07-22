"""Central keyboard shortcut definitions and base window class for all visualizers.

Import the named constants instead of raw ``Qt.Key_*`` values so that any
future re-mapping only needs to change this file.

``VisualizerWindow`` is the shared QMainWindow base every visualizer window
should inherit from.  Tab / Shift+Tab are intercepted at the QApplication
level (before Qt's focus-cycling machinery claims them) and forwarded to
the active window's ``keyPressEvent``.
"""

from PyQt5.QtCore import Qt, QEvent, QObject
from PyQt5.QtWidgets import QApplication, QMainWindow


class _AppTabFilter(QObject):
    """Application-level event filter that routes certain global shortcuts to
    the active VisualizerWindow before Qt's focus engine or child widgets
    consume them.

    Keys intercepted (with no Ctrl/Meta/Alt):
    - Escape: close the active visualizer immediately
    - Tab / Shift+Tab: panel toggle / fullscreen
    - Shift+Left / Shift+Right: page navigation

    Installed once on the QApplication instance (stored as
    ``app._visualizer_tab_filter``) so multiple windows share a single filter.
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.KeyPress:
            key = event.key()
            mod = event.modifiers()
            if mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier):
                return False
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


class VisualizerWindow(QMainWindow):
    """Base window class for all crossing-tool visualizers.

    Installs a single application-level event filter (_AppTabFilter) the
    first time any VisualizerWindow is constructed.  This filter intercepts
    Tab / Shift+Tab before Qt routes them as focus-change events, then
    forwards them to the active window's keyPressEvent.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        app = QApplication.instance()
        if app is not None and not hasattr(app, '_visualizer_tab_filter'):
            app._visualizer_tab_filter = _AppTabFilter(app)
            app.installEventFilter(app._visualizer_tab_filter)

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
