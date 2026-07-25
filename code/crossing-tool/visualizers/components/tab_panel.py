"""TabPanel — canonical container for inspector tab contents.

Responsibilities:
- Own a vertical layout for tab contents
- Provide canonical background (`theme.TAB_BG`) and 2px inset/border
- Small public API: `add_widget(widget, alignment=None)` and
  `add_section(title, widget=None, **kwargs)` which returns a
  `CollapsibleSection`.
"""
from __future__ import annotations

from typing import List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout, QWidget, QTabBar, QScrollArea

from styles import theme
from visualizers.components.collapsible_section import CollapsibleSection


class TabPanel(QWidget):
    """A thin composition surface for inspector tab content.

    The TabPanel owns the canonical tab background and a 2px border around
    its children. It contains a fixed header (tab strip) and a scrollable
    content region underneath that hosts `CollapsibleSection`s.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Paint the continuous pane background and structural border.
        self.setStyleSheet(f"background: {theme.TAB_BG}; border: 2px solid {theme.UI_BORDER};")

        # Main vertical layout — fixed header + scrollable content.
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # Header container (fixed, non-scrolling)
        self._header_container = QWidget(self)
        self._header_layout = QHBoxLayout(self._header_container)
        self._header_layout.setContentsMargins(0, 0, 0, 0)
        self._header_layout.setSpacing(0)
        # Ensure the header's unused area matches the app background and
        # has no border so it visually integrates with the chrome.
        try:
            # Use the canonical browser canvas background so the header's
            # negative space matches the browser area visually.
            self._header_container.setStyleSheet(f"background: {theme.CANVAS_BG}; border: none;")
        except Exception:
            pass
        self._layout.addWidget(self._header_container)

        # Scrollable content area beneath the header
        self._content_scroll = QScrollArea(self)
        self._content_scroll.setWidgetResizable(True)
        self._content_scroll.setFrameShape(QScrollArea.NoFrame)
        self._content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._content_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        # Use the JumpScrollBar so hover/jump/drag behaviour is improved.
        try:
            # Use the canonical JumpScrollBar implementation from theme so
            # inspector scroll behaviour and styling is identical to the
            # browser.  Referencing it via `theme` makes ownership explicit.
            self._content_scroll.setVerticalScrollBar(theme.JumpScrollBar())
        except Exception:
            pass
        # Match browser focus behaviour so the scroll area does not take
        # keyboard focus unexpectedly.
        try:
            self._content_scroll.setFocusPolicy(Qt.NoFocus)
        except Exception:
            pass
        # Ensure the scroll viewport uses the application background color
        # so the stretcher area blends into the app chrome. Keep no border.
        try:
            # The TabPanel owns the pane surface. Keep the scroll viewport
            # transparent so the TabPanel background shows through as a
            # single continuous surface (no layered frames).
            self._content_scroll.viewport().setStyleSheet("background: transparent; border: none;")
        except Exception:
            pass

        # Content widget inside the scroll area. Keep a small left/right/bottom
        # inset but no top inset so the content visually merges with the tab
        # strip above.
        self._content_widget = QWidget()
        # The content widget owns the canonical inset surface so the
        # 2px padding area behind collapsible sections matches the
        # selected tab background exactly. Keep no border here.
        try:
            self._content_widget.setStyleSheet(f"background: {theme.TAB_BG}; border: none;")
        except Exception:
            # Fall back to transparent if stylesheet application fails.
            self._content_widget.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content_widget)
        # Canonical small inset owned by the content widget so the
        # TabPanel remains the single painted surface and the scrollbar
        # stays flush to the panel edge. Apply the canonical inspector gap
        # on all sides so section bodies are inset consistently from the
        # pane chrome (left/top/right/bottom = 2px).
        self._content_layout.setContentsMargins(
            theme.INSPECTOR_GAP, theme.INSPECTOR_GAP, theme.INSPECTOR_GAP, theme.INSPECTOR_GAP
        )
        self._content_layout.setSpacing(theme.INSPECTOR_GAP)
        self._content_layout.setAlignment(Qt.AlignTop)

        self._content_scroll.setWidget(self._content_widget)
        # Remove any viewport margins so the scrollbar sits flush to the
        # panel interior. Also ensure the corner widget is transparent
        # and style the scrollbar so it has no border or padding.
        try:
            self._content_scroll.setViewportMargins(0, 0, 0, 0)
        except Exception:
            pass
        try:
            # Ensure there's no corner widget or artifact in the scroll area;
            # let the scroll area render with a transparent viewport so the
            # TabPanel visual surface remains the only painted pane.
            try:
                self._content_scroll.setCornerWidget(None)
            except Exception:
                pass
        except Exception:
            pass

        self._layout.addWidget(self._content_scroll)

        self._sections: List[CollapsibleSection] = []

        # No debug instrumentation here — TabPanel is the single
        # canonical painter of the inspector surface. Keep child
        # viewports and wrappers transparent so the TabPanel background
        # remains the single painted layer.

    def add_widget(self, widget: QWidget, alignment: int | None = None) -> None:
        """Append a simple widget to the panel.

        QTabBar instances (or widgets explicitly added with `alignment==Qt.AlignTop`)
        are placed into the fixed header area so they remain pinned. All other
        widgets are added into the scrollable content region.
        """
        try:
            if isinstance(widget, QTabBar) or alignment == Qt.AlignTop:
                base = widget.styleSheet() or ""
                widget.setStyleSheet(base + "QTabBar { background: transparent; }")
                self._header_layout.addWidget(widget)
                return
        except Exception:
            pass

        if alignment is None:
            self._content_layout.addWidget(widget)
        else:
            self._content_layout.addWidget(widget, 0, alignment)

    # TabPanel does not forward viewport mouse events to the scrollbar.
    # The `JumpScrollBar` instance manages hover, click-to-jump and drag on
    # its own — keep the standard browser pattern for consistent UX.
    def add_section(self, title: str, widget: QWidget | None = None, **kwargs) -> CollapsibleSection:
        """Create a CollapsibleSection with *title*, optionally add *widget*,
        add it to the scrollable content area and return the section."""
        section = CollapsibleSection(title, **kwargs)
        if widget is not None:
            section.add_widget(widget)
        self._content_layout.addWidget(section)
        self._sections.append(section)
        return section

    def clear(self) -> None:
        for sec in list(self._sections):
            try:
                self._content_layout.removeWidget(sec)
            except Exception:
                pass
            sec.setParent(None)
            sec.deleteLater()
        self._sections = []

    def content_scrollbar(self):
        """Return the vertical scrollbar of the scrollable content area."""
        return self._content_scroll.verticalScrollBar()
