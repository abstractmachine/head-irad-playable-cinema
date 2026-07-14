"""CollapsibleSection — Unity-Inspector-style foldable section widget.

A thin header bar (slightly different grey from the panel background) with an
arrow toggle and a title.  Clicking the header shows or hides the content
area.  Designed to replace QGroupBox throughout the Crossing visualizers.

Usage::

    sec = CollapsibleSection("Filter")
    sec.add_widget(media_combo)
    sec.add_widget(item_combo)
    parent_layout.addWidget(sec)
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
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
_HEADER_H        = 22   # px


class CollapsibleSection(QWidget):
    """A collapsible section with an arrow-toggle header bar.

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
        self._build_ui()

    # ------------------------------------------------------------------ build

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header button ─────────────────────────────────────────────────
        self._header = QPushButton()
        self._header.setFixedHeight(_HEADER_H)
        self._header.setFocusPolicy(Qt.NoFocus)
        self._header.setCheckable(False)
        self._header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._header.setStyleSheet(
            f"QPushButton {{"
            f"  background: {_HEADER_BG};"
            f"  color: {theme.TEXT};"
            f"  border: none;"
            f"  border-radius: 2px;"
            f"  text-align: left;"
            f"  padding: 0 8px;"
            f"  font-family: '{theme.FAMILY_UI}';"
            f"  font-size: {theme.BASE_PT}pt;"
            f"}}"
            f"QPushButton:hover {{ background: {_HEADER_BG_HOVER}; }}"
        )
        self._header.clicked.connect(self._toggle)
        self._refresh_header()
        outer.addWidget(self._header)

        # ── Content area ─────────────────────────────────────────────────
        self._body = QWidget()
        self._body.setStyleSheet(f"background: {_HEADER_BG};")
        self._body.setVisible(self._expanded)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 4, 0, 4)
        self._body_layout.setSpacing(4)
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
        self._body.setVisible(self._expanded)
        # Persist the new state immediately
        if self._pref_key is not None:
            try:
                from tool import prefs as _prefs
                _prefs.set(self._pref_key, self._expanded)
            except Exception:
                pass

    # ------------------------------------------------------------------ public

    def add_widget(self, widget: QWidget) -> None:
        """Append *widget* to the content area."""
        self._body_layout.addWidget(widget)

    def set_subtitle(self, subtitle: str) -> None:
        """Set a subtitle shown after the title (e.g. the active keyword).

        Pass an empty string to display only the base title.
        """
        self._subtitle = subtitle.strip()
        self._refresh_header()

    def set_expanded(self, expanded: bool) -> None:
        """Programmatically expand or collapse the section."""
        if expanded != self._expanded:
            self._toggle()
