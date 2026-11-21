"""Deferred GUI callbacks whose timer lifetime belongs to their QObject owner."""

from typing import Callable

from PyQt5.QtCore import QObject, QTimer


def defer_callback(owner: QObject, callback: Callable[[], None], delay: int = 0) -> QTimer:
    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.timeout.connect(callback)
    timer.timeout.connect(timer.deleteLater)
    timer.start(delay)
    return timer