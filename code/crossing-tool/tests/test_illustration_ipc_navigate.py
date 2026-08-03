"""Regression test for Illustration losing fullscreen state on cross-process
navigate.

Bug: Illustration always runs in its own OS process (see
`visualizers.launcher.launch_visualizer`). When another visualizer (e.g.
Project, in a *different* process) sends it a "navigate to this shot" IPC
message, `IllustrationWindow._on_ipc_navigate` used to call
`self.show(); self.showNormal()` unconditionally, which force-exited
fullscreen even when the user had deliberately left Illustration fullscreen
before switching away (F1 to Project) and back (F7 to Illustration). This is
the same class of bug already fixed for the in-process case in
`raise_existing_window()` (see `test_window_state_persistence.py`), just in
the separate code path Illustration needs because it lives in its own
process and can't be raised via `QApplication.topLevelWidgets()` there.

`_on_ipc_navigate` is invoked directly on a lightweight stand-in object
instead of a real `IllustrationWindow`, since constructing a real one
requires a full project/catalog on disk — this test only needs to verify
the window-state branch, not the catalog navigation itself.
"""

from unittest.mock import MagicMock

from visualizers.illustration_visualizer import IllustrationWindow


def _fake_window(is_fullscreen: bool) -> MagicMock:
    win = MagicMock()
    win.isFullScreen.return_value = is_fullscreen
    win._catalog = MagicMock()
    return win


def test_ipc_navigate_preserves_fullscreen() -> None:
    win = _fake_window(is_fullscreen=True)

    IllustrationWindow._on_ipc_navigate(win, "film", "field", "label", "shot", "")

    win.showFullScreen.assert_called_once()
    win.showNormal.assert_not_called()
    win.raise_.assert_called_once()
    win.activateWindow.assert_called_once()


def test_ipc_navigate_shows_windowed_when_not_fullscreen() -> None:
    win = _fake_window(is_fullscreen=False)

    IllustrationWindow._on_ipc_navigate(win, "film", "field", "label", "shot", "")

    win.show.assert_called_once()
    win.showNormal.assert_called_once()
    win.showFullScreen.assert_not_called()
    win.raise_.assert_called_once()
    win.activateWindow.assert_called_once()
