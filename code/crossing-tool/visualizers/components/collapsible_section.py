"""CollapsibleSection — shared inspector section container.

A thin header bar with arrow toggle and title. Clicking the header expands or
collapses content. This is part of the shared visualizer interaction language
and replaces ad-hoc section containers.

Usage::

    sec = CollapsibleSection("Filter")
    sec.add_widget(media_combo)
    sec.add_widget(item_combo)
    parent_layout.addWidget(sec)

Layout contract for inspector panes:
        - Parent panel spacing and margins should use theme.SECTION_GAP.
        - Section body should typically remain edge-to-edge
            (set section._body_layout contents margins to 0 when appropriate).
        - Avoid adding extra nested wrapper margins unless a section explicitly
            needs a distinct inset treatment.
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from styles import theme

# Header background — slightly darker than PANEL_BG to create a visual
# separation while keeping the interface quiet and low-contrast.
_HEADER_BG       = "#5c5c5c"
_HEADER_BG_HOVER = "#686868"
_HEADER_H        = 24   # px


class CollapsibleSection(QWidget):
    expandedChanged = pyqtSignal(bool)

    """A reusable collapsible section for inspector composition.

    Parameters
    ----------
    title:
        Label shown in the header bar.
    expanded:
        Initial expanded state.  ``True`` (default) = content visible.
    pref_key:
        Optional prefs key used to persist the expanded/collapsed state
        across app restarts.  When supplied the stored value takes priority
        over *expanded*.  Drop-down / combo selections are intentionally
        **not** managed here.
    parent:
        Optional parent widget.
    """

    def __init__(
        self,
        title: str,
        expanded: bool = True,
        pref_key: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._title    = title
        self._pref_key = pref_key
        # Restore persisted state when a pref_key is provided
        if pref_key is not None:
            try:
                from tool import prefs as _prefs
                stored = _prefs.get(pref_key)
                if stored is not None:
                    expanded = bool(stored)
            except Exception:
                pass
        self._expanded = expanded
        self._fill_vertical = False
        self._subbar: Optional[QWidget] = None
        self._header_widget: Optional[QWidget] = None
        self._apply_size_policy()
        self._build_ui()

    # ------------------------------------------------------------------ build

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header button ─────────────────────────────────────────────────
        self._header_row = QWidget()
        header_row_layout = QHBoxLayout(self._header_row)
        header_row_layout.setContentsMargins(0, 0, 0, 0)
        header_row_layout.setSpacing(0)
        self._header = QPushButton()
        self._header.setFixedHeight(_HEADER_H)
        self._header.setFocusPolicy(Qt.NoFocus)
        self._header.setCheckable(False)
        self._header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._header.setStyleSheet(
            f"QPushButton {{"
            f"  background: {theme.TITLE_BG};"
            f"  color: {theme.TEXT};"
            f"  border: none;"
            f"  border-radius: 2px;"
            f"  text-align: left;"
            f"  padding: 0 8px;"
            f"  font-family: '{theme.FAMILY_UI}';"
            f"  font-size: {theme.BASE_PT}pt;"
            f"  font-weight: {theme.WEIGHT_UI};"
            f"}}"
            f"QPushButton:hover {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
        )
        self._header.clicked.connect(self._toggle)
        self._refresh_header()
        header_row_layout.addWidget(self._header, 1)
        outer.addWidget(self._header_row)

        # Structural separator band between title and body content.
        # This is a section-level rhythm element, not a table row border.
        self._body_band = QWidget()
        self._body_band.setFixedHeight(theme.SECTION_GAP)
        self._body_band.setStyleSheet(f"background: {theme.TAB_BG};")
        self._body_band.setVisible(self._expanded)
        outer.addWidget(self._body_band)

        # ── Content area ─────────────────────────────────────────────────
        self._body = QWidget()
        self._body.setStyleSheet(f"background: {theme.TAB_BG};")
        self._body.setVisible(self._expanded)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(theme.SECTION_GAP)
        outer.addWidget(self._body)

    # ------------------------------------------------------------------ private

    def _refresh_header(self) -> None:
        arrow = "▼" if self._expanded else "▶"
        subtitle = getattr(self, "_subtitle", "")
        if subtitle:
            self._header.setText(f"{arrow}   {self._title}: {subtitle}")
        else:
            self._header.setText(f"{arrow}   {self._title}")

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh_header()
        self._body_band.setVisible(self._expanded)
        self._body.setVisible(self._expanded)
        if self._subbar is not None:
            self._subbar.setVisible(self._expanded)
        self._apply_size_policy()
        self.expandedChanged.emit(self._expanded)
        # Persist the new state immediately
        if self._pref_key is not None:
            try:
                from tool import prefs as _prefs
                _prefs.set(self._pref_key, self._expanded)
            except Exception:
                pass

    def _apply_size_policy(self) -> None:
        """Apply vertical policy: top-snapping by default, optional fill when open."""
        if self._fill_vertical and self._expanded:
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            return
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

    # ------------------------------------------------------------------ public

    def add_widget(self, widget: QWidget) -> None:
        """Append *widget* to the content area."""
        self._body_layout.addWidget(widget)

    def set_subbar(self, widget: QWidget) -> None:
        """Insert *widget* between the header and the body.

        The widget is placed in the outer layout (not inside the collapsible
        body) so it remains visible even when the section is collapsed.
        Intended for the fuchsia loading bar so it shows during loading
        regardless of whether the Filter section is open or closed.
        """
        self._subbar = widget
        widget.setParent(self)
        widget.setVisible(self._expanded)
        self.layout().insertWidget(1, widget)

    def set_subtitle(self, subtitle: str) -> None:
        """Set a subtitle shown after the title (e.g. the active keyword).

        Pass an empty string to display only the base title.
        """
        self._subtitle = subtitle.strip()
        self._refresh_header()

    def set_header_widget(self, widget: QWidget) -> None:
        """Attach an accessory widget to the right side of the title bar."""
        row_layout = self._header_row.layout()
        if self._header_widget is not None:
            row_layout.removeWidget(self._header_widget)
            self._header_widget.setParent(None)
        self._header_widget = widget
        widget.setParent(self._header_row)
        row_layout.addWidget(widget, 0, Qt.AlignRight | Qt.AlignVCenter)

    def set_expanded(self, expanded: bool) -> None:
        """Programmatically expand or collapse the section."""
        if expanded != self._expanded:
            self._toggle()

    def set_fill_vertical(self, fill: bool) -> None:
        """When enabled, expand vertically only while section body is open."""
        self._fill_vertical = bool(fill)
        self._apply_size_policy()

    def is_expanded(self) -> bool:
        """Return current expanded/collapsed state."""
        return self._expanded
