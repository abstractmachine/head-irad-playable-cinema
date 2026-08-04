"""Regression test for `_VideoReaderThread`'s Qt-object-lifetime safety.

Bug: the background capture thread (a plain daemon `threading.Thread`, not
a `QThread`) emits Qt signals via a `_VideoReaderSignals` QObject that is
parented to the owning `LiveVideoWidget`. If that widget is deleted (e.g.
the node is closed) before the thread notices `request_stop()` — which can
take longer than the caller's `wait()` grace period, since the thread only
re-checks its stop flag between blocking `select()`/`os.read()` calls —
the next `signal.emit()` call raises:

    RuntimeError: wrapped C/C++ object of type _VideoReaderSignals has
    been deleted

Uncaught, this not only prints a scary traceback but also propagates out of
`_run()` entirely, skipping the ffmpeg subprocess teardown that follows
(leaking the ffmpeg process and its V4L2 device handle). `_emit_or_stop()`
guards every emit call site against exactly this so the thread stops
gracefully and still reaches that teardown.
"""

from PyQt5 import sip
from PyQt5.QtWidgets import QApplication

from visualizers.sync_visualizer import _VideoReaderThread


def _app():
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def test_emit_or_stop_survives_deleted_signals_object():
    _app()
    # No Qt parent here (unlike the real LiveVideoWidget usage) — sip.delete()
    # on a *parented* child interferes with Qt's own parent-child deletion
    # bookkeeping and aborts the process. Using an unparented signals object
    # still exercises exactly the behavior under test: _emit_or_stop() must
    # catch the RuntimeError raised by emitting on an already-deleted QObject.
    thread = _VideoReaderThread("/dev/video0", None)

    sip.delete(thread._signals)

    assert thread._stopping is False
    ok = thread._emit_or_stop("frame_ready", object())
    assert ok is False
    assert thread._stopping is True


def test_emit_or_stop_passes_through_normally():
    _app()
    thread = _VideoReaderThread("/dev/video0", None)

    received = []
    thread.frame_ready.connect(received.append)
    ok = thread._emit_or_stop("frame_ready", "frame")
    QApplication.processEvents()

    assert ok is True
    assert thread._stopping is False
    assert received == ["frame"]
