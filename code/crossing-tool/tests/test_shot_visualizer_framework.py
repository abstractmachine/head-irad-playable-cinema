"""Smoke tests for ShotlistVisualizer's WindowVisualizer-based shell.

These exercise only the refactored window/browser/inspector architecture
(Browser | Scene | Shot | Inspector splitter, Tab toggle, shared keyboard
shortcuts) — not the underlying video/audio/annotation business logic,
which is unchanged and already covered elsewhere. Video/shotlist loading
is stubbed out entirely (no real video file is needed) so the test can
focus purely on shell construction.
"""

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeyEvent, QPixmap
from PyQt5.QtWidgets import QApplication

from styles import theme
from visualizers.shot_visualizer import ShotlistVisualizer
from visualizers.components.ipc_server import IpcServer


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


@pytest.fixture
def fake_movie(monkeypatch):
    """Stub video/shotlist loading so the window can be built without a
    real video file on disk."""
    monkeypatch.setattr(
        "data.metadata.get_metadata",
        lambda project_path, media_type=None, query=None: [
            {"filename": "movie.mp4"}
        ],
    )

    def fake_open_video(self, fatal=True):
        self.cap = None
        self.frame_rate = 24.0
        self.total_frames = 100
        self.video_native_width = 320
        self.video_native_height = 180
        self.sar_num, self.sar_den = 1, 1
        return True

    def fake_load_data(self, fatal_on_missing=True):
        self.shots = []
        self.media_id = "fake"
        self.audio_gain_db = 0.0
        self.audio_channels = None
        self._has_ann_file = False
        self.annotation_index = {}
        self._annotation_entry_index = {}
        self._embedding_row_index = {}
        self._embeddings = None
        self._embeddings_loaded = False
        self._ann_path = None
        self._ann_entries = []
        self.subtitle_cues = []
        return True

    monkeypatch.setattr(ShotlistVisualizer, "_open_video", fake_open_video)
    monkeypatch.setattr(ShotlistVisualizer, "_load_data", fake_load_data)


def _make_window(app, tmp_path):
    win = ShotlistVisualizer(str(tmp_path), ["movie.mp4"], 0, "movie", verbose=False)
    app.processEvents()
    return win


def test_window_builds_four_pane_splitter(app, fake_prefs, fake_movie, tmp_path):
    win = _make_window(app, tmp_path)
    try:
        # Browser, Scene panel, Shot panel, Inspector — flat splitter siblings.
        assert win._splitter.count() == 4
        assert win._scene_panel is not None
        assert win._shot_panel is not None
        assert win._inspector_shell is not None
    finally:
        win.close()


def test_ipc_server_uses_shared_base(app, fake_prefs, fake_movie, tmp_path):
    win = _make_window(app, tmp_path)
    try:
        assert isinstance(win._ipc_server, IpcServer)
    finally:
        win.close()


def test_subtitles_are_doubled_and_inset_inside_scaled_frame(
    app, fake_prefs, fake_movie, tmp_path
):
    win = _make_window(app, tmp_path)
    try:
        win.frame_label.resize(800, 600)
        scaled_frame = QPixmap(700, 400)

        win._position_subtitle_overlay(scaled_frame)

        margins = win._subtitle_overlay_layout.contentsMargins()
        line_spacing = win.subtitle_label.fontMetrics().lineSpacing()
        assert win.subtitle_label.font().pointSize() == theme.SUBTITLE_PT * 2
        assert margins.left() == 58
        assert margins.right() == 58
        assert margins.bottom() == 100 + line_spacing
    finally:
        win.close()


def test_tab_toggles_scene_shot_and_inspector_together(app, fake_prefs, fake_movie, tmp_path):
    win = _make_window(app, tmp_path)
    win.show()
    app.processEvents()
    try:
        assert win._scene_panel.isVisible()
        assert win._shot_panel.isVisible()
        assert win._inspector_shell.isVisible()

        event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier)
        win.keyPressEvent(event)
        app.processEvents()

        assert win._inspector_hidden is True
        assert not win._scene_panel.isVisible()
        assert not win._shot_panel.isVisible()
        assert not win._inspector_shell.isVisible()

        event2 = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier)
        win.keyPressEvent(event2)
        app.processEvents()

        assert win._inspector_hidden is False
        assert win._scene_panel.isVisible()
        assert win._shot_panel.isVisible()
        assert win._inspector_shell.isVisible()
    finally:
        win.close()


def test_escape_closes_window(app, fake_prefs, fake_movie, tmp_path):
    win = _make_window(app, tmp_path)
    win.show()
    app.processEvents()
    assert win.isVisible()

    event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    win.keyPressEvent(event)
    app.processEvents()

    assert not win.isVisible()
