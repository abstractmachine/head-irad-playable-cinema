"""Tests for the scrollbar "activity" behavior in styles/theme.py.

Covers the shared convention: scrollbars are a thin 40% grey half-width
line when idle and switch to a solid ACCENT full-width fill while hovered,
pressed/dragging, or within SCROLLBAR_ACTIVITY_MS of the last value change
for any reason (wheel, trackpad/tablet gesture, keyboard, click-to-jump,
drag-scrub, programmatic) — reverting to idle once that window elapses,
unless still hovered/dragging. The scrollbar's own footprint (SCROLLBAR_W)
must NEVER change between these two states — only the handle's look does;
resizing the container on hover causes a layout-reflow glitch.
"""

import pytest
from PyQt5.QtWidgets import QApplication

from styles import theme
from styles.theme import (
    ACCENT,
    JumpScrollBar,
    SCROLLBAR_HANDLE_IDLE_W,
    SCROLLBAR_IDLE_COLOR,
    SCROLLBAR_W,
)


@pytest.fixture
def app():
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def test_idle_style_is_narrow_and_40_percent_grey(app):
    sb = JumpScrollBar()
    try:
        assert sb.width() == SCROLLBAR_W
        assert SCROLLBAR_IDLE_COLOR in sb.styleSheet()
        assert f"{SCROLLBAR_HANDLE_IDLE_W}px solid" in sb.styleSheet()
        assert ACCENT not in sb.styleSheet()
    finally:
        sb.deleteLater()


def test_value_change_activates_color_but_not_width(app):
    sb = JumpScrollBar()
    try:
        sb.setRange(0, 100)
        sb.setValue(50)   # any value change counts as "scroll activity"

        assert sb._active is True
        assert sb.width() == SCROLLBAR_W   # footprint never changes
        assert ACCENT in sb.styleSheet()
        assert sb._activity_timer.isActive()
    finally:
        sb.deleteLater()


def test_activity_timeout_reverts_to_idle_when_not_hovered_or_dragging(app):
    sb = JumpScrollBar()
    try:
        sb.setRange(0, 100)
        sb.setValue(50)
        assert sb._active is True

        # Simulate the 0.5s decay window elapsing without real hover/drag.
        sb._activity_timer.stop()
        sb._on_activity_timeout()

        assert sb._active is False
        assert sb.width() == SCROLLBAR_W
        assert ACCENT not in sb.styleSheet()
    finally:
        sb.deleteLater()


def test_activity_timeout_keeps_active_while_dragging(app, monkeypatch):
    sb = JumpScrollBar()
    try:
        sb.setRange(0, 100)
        sb._drag_active = True
        monkeypatch.setattr(sb, "_left_button_down", lambda: True)
        sb.setValue(50)

        sb._activity_timer.stop()
        sb._on_activity_timeout()

        assert sb._active is True   # still dragging, so stays active
    finally:
        sb._drag_active = False
        sb.deleteLater()


def test_activity_timeout_clears_stale_drag_after_lost_release(app, monkeypatch):
    sb = JumpScrollBar()
    released = []
    sb.mouseReleased.connect(lambda: released.append(True))
    try:
        sb.setRange(0, 100)
        sb._drag_active = True
        monkeypatch.setattr(sb, "_left_button_down", lambda: False)
        monkeypatch.setattr(sb, "_cursor_over_bar", lambda: False)
        sb.setValue(50)

        sb._activity_timer.stop()
        sb._on_activity_timeout()

        assert sb._drag_active is False
        assert sb._active is False
        assert ACCENT not in sb.styleSheet()
        assert released == [True]
    finally:
        sb.deleteLater()


def test_global_stylesheet_recolors_without_resizing_scrollbars_on_hover(app):
    css = theme._STYLESHEET
    # Container footprint must be constant — no hover/pressed rule may
    # change QScrollBar:vertical/:horizontal's own width/height.
    assert "QScrollBar:vertical:hover" not in css
    assert "QScrollBar:vertical:pressed" not in css
    assert "QScrollBar:horizontal:hover" not in css
    assert "QScrollBar:horizontal:pressed" not in css
    assert f"width: {SCROLLBAR_W}px" in css
    assert f"height: {SCROLLBAR_W}px" in css
    # Handle recolors and switches from a half-width idle line to a
    # full-width active fill.
    assert SCROLLBAR_IDLE_COLOR in css
    assert f"{SCROLLBAR_HANDLE_IDLE_W}px solid" in css
    assert "QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed" in css
