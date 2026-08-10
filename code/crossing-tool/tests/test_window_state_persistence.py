"""Smoke tests for window fullscreen/geometry/panel-visibility persistence.

Covers three related fixes:

1. `raise_existing_window()` used to call `showNormal()` unconditionally
   when re-raising an already-open window, which force-exited fullscreen
   mode even when the window had been intentionally left fullscreen. This
   is the mechanism shared by both the Project Inspector's launcher
   buttons and the global F1-F10/F12 shortcuts (`switch_to_visualizer`),
   so it manifested as "the F# launchers don't respect the previous
   fullscreen state".

2. `WindowVisualizer` now also persists whether the inspector panel was
   hidden (plain Tab toggle) alongside geometry/fullscreen state, so
   closing and reopening a visualizer restores all three: position/size,
   fullscreen-vs-windowed, and panel visibility.

3. Restoring a window that was closed while fullscreen used to reopen
   looking merely maximized instead of truly fullscreen (some window
   managers ignore/undo a fullscreen request sent right after an initial
   normal `show()`), and used to clobber the previously-known *windowed*
   size/position with the fullscreen (screen-covering) rect, since
   `win.geometry()` returns that rect while fullscreen. `WindowVisualizer`
   now tracks its last known windowed geometry separately (via
   resizeEvent/moveEvent, and explicitly before entering fullscreen) and
   persists that instead, and `show()` goes straight to
   `showFullScreen()` when restoring a fullscreen window instead of
   calling plain `show()` first.
"""

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QLabel, QMainWindow

from visualizers.window_visualizer import WindowVisualizer


@pytest.fixture
def app():
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


@pytest.fixture
def fake_prefs(monkeypatch):
    store: dict = {}
    monkeypatch.setattr("tool.prefs.get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr("tool.prefs.set", lambda key, value: store.__setitem__(key, value))
    return store


class _DummyVisualizer(WindowVisualizer):
    def create_browser(self):
        return QLabel("browser")

    def create_inspector(self):
        return QLabel("inspector")


def _pump(app, ticks: int = 5) -> None:
    for _ in range(ticks):
        app.processEvents()


def test_raise_existing_window_preserves_fullscreen(app, fake_prefs):
    """Re-raising an already-open window must not force it out of fullscreen."""
    from visualizers._window_helpers import raise_existing_window

    win = QMainWindow()
    win.setWindowTitle("Book")
    win.show()
    win.showFullScreen()
    _pump(app)
    assert win.isFullScreen()

    try:
        assert raise_existing_window("book") is True
        _pump(app)
        assert win.isFullScreen(), "re-raising should preserve fullscreen state"
    finally:
        win.close()


def test_raise_existing_window_still_unminimizes_when_not_fullscreen(app, fake_prefs):
    """Non-fullscreen windows should still be restored from minimized state."""
    from visualizers._window_helpers import raise_existing_window

    win = QMainWindow()
    win.setWindowTitle("Book")
    win.show()
    win.showMinimized()
    _pump(app)

    try:
        assert raise_existing_window("book") is True
        _pump(app)
        assert not win.isMinimized()
        assert not win.isFullScreen()
    finally:
        win.close()


def test_window_visualizer_persists_geometry_fullscreen_and_panel_visibility(app, fake_prefs):
    win1 = _DummyVisualizer(pref_key="window_test_dummy")
    win1.show()
    _pump(app)

    # Enter fullscreen (Shift+Tab) and hide the inspector panel (Tab).
    win1.keyPressEvent(_make_key_event(Qt.Key_Tab, Qt.ShiftModifier))
    _pump(app)
    assert win1.isFullScreen()

    win1.keyPressEvent(_make_key_event(Qt.Key_Tab, Qt.NoModifier))
    _pump(app)
    assert win1._inspector_hidden is True
    assert win1._inspector_shell.isVisible() is False

    win1.close()
    _pump(app)

    saved = fake_prefs["window_test_dummy"]
    assert len(saved) == 6
    assert saved[4] == 1  # fullscreen flag
    assert saved[5] == 1  # panel-hidden flag

    win2 = _DummyVisualizer(pref_key="window_test_dummy")
    win2.show()
    _pump(app, ticks=10)

    assert win2.isFullScreen()
    assert win2._inspector_hidden is True
    assert win2._inspector_shell.isVisible() is False

    win2.close()


def test_windowed_geometry_preserved_when_closed_while_fullscreen(app, fake_prefs):
    """Closing a window while fullscreen must not clobber its previously-
    known windowed size/position with the fullscreen (screen-covering)
    rect — `win.geometry()` returns that rect while fullscreen, so the
    tracked pre-fullscreen geometry must be used instead when saving."""
    win = _DummyVisualizer(pref_key="window_test_geom_fs")
    win.show()
    _pump(app)

    win.setGeometry(50, 60, 700, 500)
    _pump(app)
    assert not win.isFullScreen()

    win.keyPressEvent(_make_key_event(Qt.Key_Tab, Qt.ShiftModifier))
    _pump(app)
    assert win.isFullScreen()

    win.close()
    _pump(app)

    saved = fake_prefs["window_test_geom_fs"]
    assert saved[:4] == [50, 60, 700, 500]
    assert saved[4] == 1

    # Reopening must start truly fullscreen (via show()'s pending-fullscreen
    # path, not a plain show() call) and, once toggled back to windowed via
    # Shift+Tab, must land on the originally-saved windowed geometry.
    win2 = _DummyVisualizer(pref_key="window_test_geom_fs")
    win2.show()
    _pump(app, ticks=10)
    assert win2.isFullScreen()

    win2.keyPressEvent(_make_key_event(Qt.Key_Tab, Qt.ShiftModifier))
    _pump(app)
    assert not win2.isFullScreen()
    g = win2.geometry()
    assert (g.x(), g.y(), g.width(), g.height()) == (50, 60, 700, 500)

    win2.close()


def test_show_goes_straight_to_fullscreen_when_pending(app, fake_prefs):
    """show() must consume `_pending_fullscreen` and call showFullScreen()
    directly rather than showing normal first — that intermediate state is
    what let some window managers leave the window merely maximized."""
    win = _DummyVisualizer(pref_key="window_test_pending_fs")
    win.show()
    win.keyPressEvent(_make_key_event(Qt.Key_Tab, Qt.ShiftModifier))
    _pump(app)
    win.close()
    _pump(app)

    win2 = _DummyVisualizer(pref_key="window_test_pending_fs")
    assert win2._pending_fullscreen is True
    win2.show()
    # No event-loop pump yet: show() itself must have already gone
    # fullscreen synchronously, not deferred it to a later tick.
    assert win2.isFullScreen()
    assert win2._pending_fullscreen is False

    win2.close()


def test_illustration_preserves_saved_windowed_geometry(app, fake_prefs, monkeypatch, tmp_path):
    from PyQt5.QtCore import QRect
    from PyQt5.QtWidgets import QApplication
    from visualizers.illustration_visualizer import IllustrationWindow, _IllIpcServer

    class _DesktopScreen:
        @staticmethod
        def availableGeometry():
            return QRect(0, 0, 1920, 1080)

    monkeypatch.setattr(
        QApplication, "primaryScreen", staticmethod(lambda: _DesktopScreen())
    )
    monkeypatch.setattr(_IllIpcServer, "start", lambda self: None)
    monkeypatch.setattr(_IllIpcServer, "stop", lambda self: None)
    monkeypatch.setattr(_IllIpcServer, "wait", lambda self, timeout=0: True)

    win1 = IllustrationWindow(str(tmp_path), media_type=None)
    win1.show()
    _pump(app)
    win1.setGeometry(70, 80, 1100, 680)
    _pump(app)
    win1.close()
    _pump(app)

    assert fake_prefs["window_illustration"][:4] == [70, 80, 1100, 680]

    win2 = IllustrationWindow(str(tmp_path), media_type=None)
    win2.show()
    _pump(app, ticks=10)
    geometry = win2.geometry()
    assert (
        geometry.x(), geometry.y(), geometry.width(), geometry.height()
    ) == (70, 80, 1100, 680)
    win2.close()


def _make_key_event(key, modifiers):
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtCore import QEvent
    return QKeyEvent(QEvent.KeyPress, key, modifiers)
