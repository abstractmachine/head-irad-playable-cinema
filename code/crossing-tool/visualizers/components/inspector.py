"""Inspector — simple reusable inspector composition surface.

Responsibilities:
 - Own a QScrollArea and a vertical layout for composing CollapsibleSections
 - Provide consistent spacing and a small public API: add_group() and clear()

The Inspector intentionally knows nothing about the content it hosts.
"""
from __future__ import annotations

from typing import List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from styles import theme
from visualizers.components.collapsible_section import CollapsibleSection


class Inspector(QWidget):
    """A minimal inspector composition surface.

    Public API:
      - add_group(title: str, widget: QWidget) -> CollapsibleSection
      - clear()
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Outer scroll area owned by the inspector
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setStyleSheet(f"QScrollArea {{ background: {theme.CANVAS_BG}; border: none; }}")

        # Content widget and layout
        self.content = QWidget()
        self.content.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self.content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

        self._layout = QVBoxLayout(self.content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(theme.SECTION_GAP)
        self._layout.setAlignment(Qt.AlignTop)

        self._groups: List[CollapsibleSection] = []

        self.scroll.setWidget(self.content)

        # Layout for this Inspector widget — simply host the scroll area
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.scroll)

    def add_widget(self, widget: QWidget, alignment: int | None = None) -> None:
        """Append a plain widget to the inspector content area.

        This is a small convenience so callers do not need to reach into
        the inspector's internal `content` widget or layout.
        """
        if alignment is None:
            self._layout.addWidget(widget)
        else:
            self._layout.addWidget(widget, 0, alignment)

    def set_minimum_width(self, width: int) -> None:
        """Set the minimum width of the inspector content widget."""
        try:
            self.content.setMinimumWidth(int(width))
        except Exception:
            pass

    def add_group(self, title: str, widget: QWidget | None = None, **kwargs) -> CollapsibleSection:
        """Create a CollapsibleSection with *title*, optionally add *widget* into it,
        add it to the inspector layout and return the section."""
        section = CollapsibleSection(title, **kwargs)
        section.setStyleSheet(f"background: {theme.TAB_BG};")
        if widget is not None:
            section.add_widget(widget)
        self._layout.addWidget(section)
        self._groups.append(section)
        return section

    def clear(self) -> None:
        """Remove and delete all groups added to this inspector."""
        for sec in list(self._groups):
            try:
                self._layout.removeWidget(sec)
            except Exception:
                pass
            sec.setParent(None)
            sec.deleteLater()
        self._groups = []
