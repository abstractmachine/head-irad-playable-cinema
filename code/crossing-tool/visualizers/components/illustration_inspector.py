"""IllustrationInspector — extensible illustration detail panel.

The Inspector shows everything currently known about the selected illustration.
It is extensible: each visualizer can add its own sections to the vertical
layout without modifying this base class.

Design
------
The Inspector observes the Browser's ``selectionChanged`` signal and calls
``setItem()`` whenever the selection changes.  Nothing should communicate
directly with the Inspector — all updates flow through the Browser selection.

The Inspector intentionally does NOT define a section registry or plugin
framework.  Qt layouts already provide composition.  Each visualizer
constructs the Inspector and adds its own widgets to the layout.

Example::

    inspector = IllustrationInspector()
    inspector.addSection(metadata_block)   # any QWidget
    inspector.addSection(scores_block)     # any QWidget

    # Connect to browser selection
    browser.selectionChanged.connect(inspector.setItem)

Phase status
------------
Phase 1: Public API established.  ``setItem`` is a no-op placeholder.
Concrete section additions are deferred to Phase 4 visualizer migration.
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from styles import theme


class IllustrationInspector(QWidget):
    """Extensible panel that inspects the selected illustration.

    Parameters
    ----------
    parent:
        Optional parent widget.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._item: Optional[dict] = None

        self._build_ui()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scroll area so the inspector can host arbitrarily many sections.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {theme.PANEL_BG}; border: none; }}"
        )

        self._content = QWidget()
        self._content.setStyleSheet(f"background: {theme.PANEL_BG};")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(8)
        self._layout.setAlignment(Qt.AlignTop)

        # TODO (Phase 4): sections added by each concrete visualizer will
        # appear here.  The placeholder below will be removed when the first
        # real section is added.
        self._placeholder = QLabel("No illustration selected.")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"
        )
        self._placeholder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._layout.addWidget(self._placeholder)

        scroll.setWidget(self._content)
        outer.addWidget(scroll)

    # ------------------------------------------------------------------ public API

    def setItem(self, item: Optional[dict]) -> None:
        """Update the inspector to display *item*.

        Called automatically when ``IllustrationBrowser.selectionChanged``
        fires.  Each section widget in ``_layout`` should be updated by
        the concrete visualizer's wiring — this base method simply stores
        the item for reference.
        """
        self._item = item
        if item is None:
            self._placeholder.setText("No illustration selected.")
        else:
            self._placeholder.setText("")

    def addSection(self, widget: QWidget) -> None:
        """Append *widget* as a new section below existing sections.

        The visualizer calls this at construction time to add its own content
        (e.g. a MetadataBlock, a scores panel, a generation controls widget).
        """
        # Hide the placeholder once real content is present.
        self._placeholder.hide()
        self._layout.addWidget(widget)

    def currentItem(self) -> Optional[dict]:
        """Return the currently displayed item dict, or ``None``."""
        return self._item
