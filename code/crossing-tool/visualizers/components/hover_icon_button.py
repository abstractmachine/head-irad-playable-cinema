"""Shared hover-aware icon button primitives for visualizer panels."""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QPushButton

from styles import theme
from styles.theme import svg_icon as _svg_icon


class HoverIconButton(QPushButton):
    """QPushButton that swaps icon on hover and keeps hover icon while checked."""

    def __init__(self, text: str = "", normal_icon: QIcon | None = None, hover_icon: QIcon | None = None, parent=None):
        super().__init__(text, parent)
        self._normal_icon = normal_icon
        self._hover_icon = hover_icon
        self.setFocusPolicy(Qt.NoFocus)
        if normal_icon is not None:
            self.setIcon(normal_icon)

    def set_icons(self, normal_icon: QIcon | None, hover_icon: QIcon | None) -> None:
        self._normal_icon = normal_icon
        self._hover_icon = hover_icon
        if self._normal_icon is not None:
            self.setIcon(self._normal_icon)

    def enterEvent(self, event):  # noqa: N802
        if self._hover_icon and self.isEnabled():
            self.setIcon(self._hover_icon)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        if self._normal_icon and not self.isChecked():
            self.setIcon(self._normal_icon)
        super().leaveEvent(event)

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        super().setChecked(checked)
        if checked and self._hover_icon:
            self.setIcon(self._hover_icon)
        elif not checked and self._normal_icon:
            self.setIcon(self._normal_icon)


def build_icon_pair(
    svg_name: str,
    size: int,
    normal_color: str = theme.TEXT,
    hover_color: str = theme.ACCENT_TEXT,
    disabled_color: str = "#7f7f7f",
) -> tuple[QIcon, QIcon]:
    """Return (normal_icon, hover_icon) with proper disabled pixmaps."""
    disabled_pm = _svg_icon(svg_name, size, disabled_color).pixmap(size, size)

    normal = QIcon()
    normal.addPixmap(_svg_icon(svg_name, size, normal_color).pixmap(size, size))
    normal.addPixmap(disabled_pm, QIcon.Disabled)

    hover = QIcon()
    hover.addPixmap(_svg_icon(svg_name, size, hover_color).pixmap(size, size))
    hover.addPixmap(disabled_pm, QIcon.Disabled)

    return normal, hover
