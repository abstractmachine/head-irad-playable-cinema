from __future__ import annotations

from PyQt5.QtCore import QEvent, QObject, Qt
from PyQt5.QtGui import QColor, QWheelEvent
from PyQt5.QtWidgets import (
    QAbstractScrollArea, QApplication, QComboBox, QListView, QFrame,
)

from styles import theme


ALL_DISPLAY_TEXT = "<all>"


class _ClosedComboWheelForwarder(QObject):
    """Keep Inspector wheel scrolling active while a combo popup is closed."""

    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo)
        self._combo = combo

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() != QEvent.Wheel or self._combo.view().isVisible():
            return False
        scroll_area = self._nearest_scroll_area()
        if scroll_area is None:
            return False
        global_pos = self._combo.mapToGlobal(event.pos())
        target = scroll_area.viewport()
        forwarded = QWheelEvent(
            target.mapFromGlobal(global_pos),
            global_pos,
            event.pixelDelta(),
            event.angleDelta(),
            event.buttons(),
            event.modifiers(),
            event.phase(),
            event.inverted(),
            event.source(),
        )
        QApplication.postEvent(target, forwarded)
        return True

    def _nearest_scroll_area(self) -> QAbstractScrollArea | None:
        parent = self._combo.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                return parent
            parent = parent.parentWidget()
        return None


def add_combo_all_item(combo: QComboBox, user_data=None) -> None:
    """Add a dimmed ``<all>`` display item while preserving caller data."""
    combo.addItem(ALL_DISPLAY_TEXT, userData=user_data)
    combo.setItemData(combo.count() - 1, QColor(theme.TEXT_DIM), Qt.ForegroundRole)
    if combo.property("crossingCanonicalCombo"):
        _refresh_combo_text_color(combo)


def _refresh_combo_text_color(combo: QComboBox) -> None:
    color = theme.TEXT_DIM if combo.currentText() == ALL_DISPLAY_TEXT else theme.TEXT
    combo.setStyleSheet(canonical_combo_stylesheet(color))


def attach_combo_popup(combo: QComboBox) -> QListView:
    """Attach a styled QListView popup to *combo* and return the view.

    Centralized popup styling/cleanup used by visualizers that need a
    consistent combo-list appearance. This mirrors the previous inline
    implementations: it creates the QListView, applies the popup stylesheet,
    removes frame/borders/margins, and attempts to clean up the container
    widget Qt may create for the popup.
    """
    _sv = QListView(combo)
    _sv.setUniformItemSizes(True)
    _sv.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    _sv.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    _sv.setVerticalScrollBar(theme.JumpScrollBar())
    _sv.setFrameShape(QFrame.NoFrame)
    _sv.setLineWidth(0)
    _sv.setMidLineWidth(0)
    _sv.setContentsMargins(0, 0, 0, 0)
    # Ensure the combo's displayed text is bold and the popup items match.
    try:
        combo.setFont(theme.font_ui(bold=True))
    except Exception:
        pass

    _sv.setStyleSheet(
        f"QListView {{ background: {theme.INPUT_BG}; color: {theme.TEXT};"
        f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt; font-weight: bold;"
        f" border: 0px; margin: 0px; padding: 0px; outline: 0px; }}"
        f"QListView::item {{ background: {theme.INPUT_BG}; padding: 0px 8px;"
        f" min-height: 24px; border: 0px; font-weight: bold; }}"
        f"QListView::item:selected {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; font-weight: bold; }}"
    )
    combo.setView(_sv)
    _sv.setViewportMargins(0, 0, 0, 0)
    _sc = _sv.parentWidget()
    if _sc is not None:
        try:
            _sc.setFrameStyle(QFrame.NoFrame)
            _sc.setLineWidth(0)
            _sc.setMidLineWidth(0)
            _sc.setStyleSheet(f"QFrame {{ background: {theme.INPUT_BG}; border: 0px; margin: 0px; padding: 0px; }}")
            if _sc.layout():
                _sc.layout().setContentsMargins(0, 0, 0, 0)
                _sc.layout().setSpacing(0)
        except Exception:
            pass
    return _sv


def canonical_combo_stylesheet(text_color: str | None = None) -> str:
    """Return the canonical QComboBox stylesheet string.

    *text_color* overrides the displayed text color (e.g. `theme.TEXT_DIM`
    for a placeholder/empty state); defaults to `theme.TEXT`. Exposed
    separately from `style_canonical_combo()` so callers can re-color a
    combo in place (e.g. when toggling a dimmed placeholder item) without
    re-attaching the popup view each time.
    """
    color = text_color or theme.TEXT
    return (
        f"QComboBox {{"
        f"  background: {theme.BTN_BG}; color: {color};"
        f"  border: none; border-radius: 3px; padding: 0px 6px;"
        f"  min-height: 24px; max-height: 24px;"
        f"  font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
        f"  font-weight: {theme.WEIGHT_UI};"
        f"}}"
        f"QComboBox::drop-down {{ border: none; }}"
    )


def style_canonical_combo(combo: QComboBox) -> None:
    """Apply the canonical visualizer combo box font, sizing, and popup styling.

    Fixes the "QComboBox with a long item text silently inflates its
    container's minimum width" class of bug: pins the combo's own width via
    `AdjustToMinimumContentsLength` instead of letting it scale with the
    longest item. The popup list is styled via the shared
    `attach_combo_popup()` above, so this and every other canonical combo in
    the app share one popup implementation. Any visualizer combo box should
    use this instead of inventing its own popup styling.
    """
    combo.setFocusPolicy(Qt.NoFocus)
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLength)
    combo.setProperty("crossingCanonicalCombo", True)
    combo.currentIndexChanged.connect(lambda _index, target=combo: _refresh_combo_text_color(target))
    _refresh_combo_text_color(combo)
    attach_combo_popup(combo)
    if combo.property("crossingClosedWheelForwarder") is None:
        forwarder = _ClosedComboWheelForwarder(combo)
        combo.installEventFilter(forwarder)
        combo.setProperty("crossingClosedWheelForwarder", forwarder)
