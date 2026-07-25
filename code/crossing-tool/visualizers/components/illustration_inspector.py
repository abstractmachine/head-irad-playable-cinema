"""IllustrationInspector — compositional selection detail surface.

The inspector presents information and controls for the browser-selected item.
It is intentionally compositional: visualizers add reusable sections instead
of replacing inspector infrastructure.

Design
------
The inspector reacts to ``IllustrationBrowser.selectionChanged`` via
``setItem()``. Selection ownership stays in the browser; the inspector does
not own or arbitrate selection.

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
from PyQt5.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from styles import theme
from visualizers.components.inspector import Inspector


class IllustrationInspector(QWidget):
    """Extensible panel for selected-item presentation and controls.

    This class owns how the selected record is presented, not which record is
    selected. Controls hosted here should invoke existing services/CLI-backed
    operations rather than duplicating project logic.

    Parameters
    ----------
    parent:
        Optional parent widget.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._item: Optional[dict] = None

        self._inspector = Inspector(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._inspector)
        # Placeholder label is added into the inspector panel until real
        # sections are appended by callers.
        self._placeholder = QLabel("No illustration selected.")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;")
        self._placeholder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._inspector.panel().add_widget(self._placeholder)

    # ------------------------------------------------------------------ UI

    def addSection(self, widget: QWidget) -> None:
        """Append *widget* as a new section below existing sections.

        Visualizers compose inspector behavior by adding sections
        (e.g. MetadataBlock, score panels, operation controls). This method
        avoids custom inspector rewrites per visualizer.
        """
        # Hide the placeholder once real content is present.
        try:
            self._placeholder.hide()
        except Exception:
            pass
        self._inspector.panel().add_widget(widget)

    # ------------------------------------------------------------------ public API

    def setItem(self, item: Optional[dict]) -> None:
        """Update the inspector to display *item*.

        Called automatically when ``IllustrationBrowser.selectionChanged``
        fires.  Each section widget in ``_layout`` should be updated by
        the concrete visualizer's wiring — this base method simply stores
        the item for reference.

        Passing ``None`` represents "no browser selection".
        """
        self._item = item
        if item is None:
            try:
                self._placeholder.setText("No illustration selected.")
            except Exception:
                pass
        else:
            try:
                self._placeholder.setText("")
            except Exception:
                pass

    def addSection(self, widget: QWidget) -> None:
        # Deprecated alias for backwards compatibility.
        self._inspector.panel().add_widget(widget)

    def currentItem(self) -> Optional[dict]:
        """Return the currently displayed item dict, or ``None``."""
        return self._item
