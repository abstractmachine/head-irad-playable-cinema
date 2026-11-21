"""Real-thread, socket and deferred-call ownership regressions."""

import socket
import threading
import time

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QObject, QThread
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget

from visualizers.components.deferred import defer_callback
from visualizers.components.ipc_server import IpcServer
from visualizers.components.thumbnail_manager import ThumbnailManager
from visualizers.window_visualizer import WindowVisualizer


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path, app):
    existing = set(app.topLevelWidgets())
    store = {"path": str(tmp_path)}
    monkeypatch.setattr("tool.prefs.get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr("tool.prefs.set", lambda key, value: store.__setitem__(key, value))
    yield
    for window in set(app.topLevelWidgets()) - existing:
        if not sip.isdeleted(window):
            window.close()
            window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


class LifecycleWindow(WindowVisualizer):
    def create_browser(self):
        return QWidget()

    def create_inspector(self):
        return QWidget()


class WaitingWorker(QThread):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.entered = threading.Event()
        self.cancelled = threading.Event()
        self.terminate_requested = False
        self.observed_interruption = False

    def cancel(self):
        self.cancelled.set()

    def terminate(self):
        self.terminate_requested = True
        self.cancel()

    def run(self):
        self.entered.set()
        while not self.isInterruptionRequested() and not self.cancelled.wait(0.01):
            pass
        self.observed_interruption = self.isInterruptionRequested()


@pytest.mark.parametrize("parented", [False, True, "nested"])
def test_window_close_joins_owned_workers(parented, app):
    window = LifecycleWindow()
    owner = window._browser if parented == "nested" else window
    worker = WaitingWorker(window if parented is True else None)
    owner._worker = worker
    worker.start()
    try:
        assert worker.entered.wait(1)
        window.close()
        assert not worker.isRunning()
        assert worker.observed_interruption
        assert not worker.terminate_requested
    finally:
        worker.cancel()
        assert worker.wait(1000)
        worker.deleteLater()


def test_window_close_quits_owned_thread_event_loop(app):
    window = LifecycleWindow()
    worker = QThread(window)
    worker.start()
    try:
        window.close()
        assert worker.wait(1000)
        assert not worker.isRunning()
    finally:
        worker.quit()
        assert worker.wait(1000)


def test_deferred_callback_is_cancelled_with_owner(app):
    owner = QWidget()
    calls = []
    timer = defer_callback(owner, lambda: calls.append("called"))
    assert timer.parent() is owner
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert calls == []
    assert sip.isdeleted(timer)


def test_deferred_callback_releases_timer_after_delivery(app):
    owner = QWidget()
    calls = []
    timer = defer_callback(owner, lambda: calls.append("called"))
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert calls == ["called"]
    assert sip.isdeleted(timer)


def test_metadata_deferred_controls_stop_at_destruction(app, tmp_path, monkeypatch):
    from visualizers.metadata_visualizer import MetadataVisualizer

    monkeypatch.setattr("data.metadata.get_metadata", lambda *_args, **_kwargs: [])
    calls = []
    monkeypatch.setattr(MetadataVisualizer, "_active_media_type", lambda _window: "movie")
    monkeypatch.setattr(MetadataVisualizer, "_update_thumbnail_preview", lambda *_args: calls.append("thumbnail"))
    monkeypatch.setattr(MetadataVisualizer, "_update_zoom_buttons", lambda *_args: calls.append("zoom"))
    window = MetadataVisualizer(str(tmp_path))
    window.show()
    window.resize(1000, 650)
    window._toggle_inspector()
    window._change_zoom("movie", 0.4)
    window.close()
    calls.clear()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert sip.isdeleted(window)
    assert calls == []


def test_browser_deferred_navigation_stops_at_destruction(app, tmp_path, monkeypatch):
    from visualizers.components.illustration_browser import IllustrationBrowser

    browser = IllustrationBrowser(str(tmp_path), media_type=None)
    browser._has_completed_catalog_load = True
    browser.navigate_to_filters(item="film", field="objects")
    calls = []
    monkeypatch.setattr(browser, "navigate_to_filters", lambda **values: calls.append(values))
    browser.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QTest.qWait(300)
    assert sip.isdeleted(browser)
    assert calls == []


def test_thumbnail_stop_joins_blocked_resolver(app):
    entered = threading.Event()
    release = threading.Event()

    def resolve(_record):
        entered.set()
        assert release.wait(2)
        return None

    manager = ThumbnailManager()
    manager.start([{}], 32, resolve, lambda *_args: None)
    loader = manager._loader
    unblock = threading.Timer(0.45, release.set)
    unblock.start()
    try:
        assert entered.wait(1)
        manager.stop()
        assert not loader.isRunning()
        assert manager._loader is None
    finally:
        release.set()
        unblock.join()
        assert loader.wait(1000)
        manager.stop()
        loader.deleteLater()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.parametrize("partial_message", [b"", b'{"action":'])
def test_ipc_stop_unblocks_connected_peer(partial_message, app, tmp_path, monkeypatch):
    reading = threading.Event()
    delivered = []
    socket_class = socket.socket

    class ObservedSocket(socket_class):
        def recv(self, *arguments):
            reading.set()
            return super().recv(*arguments)

    class Server(IpcServer):
        def _handle_message(self, message):
            delivered.append(message)

    monkeypatch.setattr(socket, "socket", ObservedSocket)
    path = tmp_path / "lifecycle.sock"
    server = Server(path)
    server.start()
    peer = socket_class(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        deadline = time.monotonic() + 1
        while not path.exists() and time.monotonic() < deadline:
            QTest.qWait(5)
        assert path.exists()
        peer.connect(str(path))
        if partial_message:
            peer.sendall(partial_message)
        assert reading.wait(1)
        server.stop()
        assert server.wait(500)
        assert delivered == []
        assert not path.exists()
    finally:
        peer.close()
        server.stop()
        assert server.wait(1000)
        server.deleteLater()


def test_project_replacement_joins_previous_worker(app, monkeypatch):
    from visualizers.project_visualizer import ProjectVisualizer

    entered = threading.Event()
    release = threading.Event()

    def load_live(_project):
        entered.set()
        assert release.wait(2)
        return []

    monkeypatch.setattr("services.corpus_stats.get_live_project_columns", load_live)
    monkeypatch.setattr("services.corpus_stats.get_cached_project_columns", lambda *_args: [])
    monkeypatch.setattr("services.backup_mirror.backup_status", lambda *_args: {})
    window = ProjectVisualizer()
    previous = window._project_columns_worker
    unblock = threading.Timer(0.2, release.set)
    unblock.start()
    try:
        assert entered.wait(1)
        window._start_project_columns_load(force=True)
        assert previous.parent() is window
        assert not previous.isRunning()
        assert window._project_columns_worker is not previous
        assert window._project_columns_worker.parent() is window
    finally:
        release.set()
        unblock.join()
        window.close()


def test_singleton_guard_joins_when_application_quits(app, tmp_path):
    from visualizers.components.singleton_guard import claim_or_ping_and_bind

    try:
        assert claim_or_ping_and_bind("lifecycle", str(tmp_path), app)
        guard = app._singleton_guards["lifecycle"]
        app.aboutToQuit.emit()
        assert not guard.isRunning()
    finally:
        guard = app._singleton_guards.pop("lifecycle", None)
        if guard is not None:
            guard.shutdown()
            guard.deleteLater()


def test_live_video_stop_joins_before_releasing_reader(app, monkeypatch):
    from visualizers.sync_visualizer import LiveVideoWidget, _VideoReaderThread

    entered = threading.Event()
    release = threading.Event()

    def read(_reader):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(_VideoReaderThread, "_run", read)
    widget = LiveVideoWidget("unused-device")
    reader = widget._thread
    unblock = threading.Timer(3.2, release.set)
    unblock.start()
    try:
        assert entered.wait(1)
        widget.stop()
        assert not reader.isRunning()
        assert widget._thread is None
    finally:
        release.set()
        unblock.join()
        reader.request_stop()
        assert reader.wait(1000)
        widget.stop()
        widget.deleteLater()


def test_segmentation_close_is_cooperative(app, tmp_path, monkeypatch):
    from visualizers.segmentation_visualizer import SAMExplorer

    monkeypatch.setattr("data.metadata.get_metadata", lambda *_args, **_kwargs: [])
    window = SAMExplorer(str(tmp_path))
    worker = WaitingWorker()
    window._seg_worker = worker
    worker.start()
    try:
        assert worker.entered.wait(1)
        window.close()
        assert not worker.terminate_requested
        assert worker.observed_interruption
        assert not worker.isRunning()
    finally:
        worker.cancel()
        assert worker.wait(1000)
        worker.deleteLater()