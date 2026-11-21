"""Keep one QApplication alive and dispose test-owned Qt work before teardown."""

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QThread, QTimer
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="session", autouse=True)
def qt_application():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def qt_test_cleanup(qt_application, monkeypatch):
    top_level_widgets = QApplication.topLevelWidgets
    existing = set(top_level_widgets())
    previous_guards = set(getattr(qt_application, "_singleton_guards", {}))
    yield
    widgets = [widget for widget in set(top_level_widgets()) - existing
               if not sip.isdeleted(widget)]
    workers = set()
    for widget in widgets:
        for timer in widget.findChildren(QTimer):
            timer.stop()
        workers.update(widget.findChildren(QThread))
        workers.update(value for value in vars(widget).values() if isinstance(value, QThread))
    for worker in workers:
        if sip.isdeleted(worker):
            continue
        worker.requestInterruption()
        for method_name in ("cancel", "stop"):
            method = getattr(worker, method_name, None)
            if callable(method):
                method()
        worker.quit()
    for worker in workers:
        if not sip.isdeleted(worker):
            assert worker.wait(5000), "test-owned Qt worker did not stop"
    for widget in widgets:
        if not sip.isdeleted(widget):
            widget.close()
    for widget in widgets:
        if not sip.isdeleted(widget):
            widget.deleteLater()
    guards = getattr(qt_application, "_singleton_guards", {})
    for name in set(guards) - previous_guards:
        guard = guards.pop(name)
        if not sip.isdeleted(guard):
            guard.shutdown()
            guard.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)