"""TimelineScrubber — shared bottom scrub bar, factored out of Shotlist.

This is a verbatim extraction of the `_TimelineScrollBar` / `_TimelineHitArea`
pair that lived inside ``visualizers/shot_visualizer.py``. Behaviour is
unchanged: Shotlist imports these classes from here, and Palette reuses the
same widgets rather than growing a second scrubber implementation.

`TimelineHitArea` gives the thin `JumpScrollBar` a fifteen-bar-high mouse
target so the scrubber is easy to grab, while the visible control keeps its
canonical `theme.SCROLLBAR_W` footprint — that footprint must never change
between idle/hover/active states.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QPointF
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QApplication, QWidget

from styles import theme
from styles.theme import JumpScrollBar


class TimelineScrollBar(JumpScrollBar):
    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Horizontal, parent)
        self._hit_area = None

    def _on_value_changed(self, _value: int) -> None:
        """Keep timeline emphasis hover/drag-driven during video playback."""
        self._activity_timer.stop()
        self._set_active(self._drag_active or self._cursor_over_bar())

    def _cursor_over_bar(self) -> bool:
        if self._hit_area is None:
            return super()._cursor_over_bar()
        local = self._hit_area.mapFromGlobal(self.cursor().pos())
        return self._hit_area.rect().contains(local)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        # Moving from the visible bar into the larger host must still count
        # as hovering the timeline, even though the child itself was left.
        if self._cursor_over_bar():
            self._set_active(True)


class TimelineHitArea(QWidget):
    """Generous mouse target around the unchanged timeline control.

    *bars* is the target's height as a multiple of ``theme.SCROLLBAR_W``. The
    default of 15 is Shotlist's original value, where the scrubber sits under a
    video player with room to spare. A host with less vertical room (a browser
    grid, say) should pass a smaller value rather than leaving a tall empty
    band. The visible control keeps its canonical ``SCROLLBAR_W`` footprint
    either way.
    """

    def __init__(self, scrollbar: TimelineScrollBar, parent=None, *,
                 bars: int = 15) -> None:
        super().__init__(parent)
        self._scrollbar = scrollbar
        self._scrollbar.setParent(self)
        self._scrollbar._hit_area = self
        self.setFixedHeight(theme.SCROLLBAR_W * max(1, int(bars)))
        self.setMouseTracking(True)

    def scrollbar(self) -> TimelineScrollBar:
        return self._scrollbar

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._scrollbar.setGeometry(
            0,
            self.height() - theme.SCROLLBAR_W,
            self.width(),
            theme.SCROLLBAR_W,
        )

    def enterEvent(self, event) -> None:
        self._scrollbar._set_active(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._scrollbar._activity_timer.stop()
        if not self._scrollbar._drag_active:
            self._scrollbar._set_active(False)
        super().leaveEvent(event)

    def _forward_mouse_event(self, event) -> None:
        local = QPointF(
            event.pos().x(),
            min(theme.SCROLLBAR_W - 1, max(0, event.pos().y() - self._scrollbar.y())),
        )
        forwarded = QMouseEvent(
            event.type(),
            local,
            event.button(),
            event.buttons(),
            event.modifiers(),
        )
        QApplication.sendEvent(self._scrollbar, forwarded)
        event.accept()

    def mousePressEvent(self, event) -> None:
        self._forward_mouse_event(event)

    def mouseMoveEvent(self, event) -> None:
        self._forward_mouse_event(event)

    def mouseReleaseEvent(self, event) -> None:
        self._forward_mouse_event(event)
