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

from PyQt5.QtCore import Qt, pyqtSignal, QEvent
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
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
        # When True the subbar is considered persistent and remains visible
        # even when the section body is collapsed. This is used for the
        # canonical filter-loading indicator which should be visible on the
        # section header regardless of expanded state.
        self._subbar_persistent: bool = False
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
        # Ensure the header row paints its own background (slightly darker
        # than the section body) so the title area reads as a distinct band.
        try:
            self._header_row.setStyleSheet(f"background: {theme.TITLE_BG}; border: none;")
        except Exception:
            pass
        self._header = QPushButton()
        self._header.setFixedHeight(_HEADER_H)
        self._header.setFocusPolicy(Qt.NoFocus)
        self._header.setCheckable(False)
        self._header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Header renders a distinct title band. Keep the header background
        # on the header itself, but do not paint the section body — the
        # TabPanel owns the canonical pane background.
        self._header.setStyleSheet(
            f"QPushButton {{"
            f"  background: {theme.TITLE_BG};"
            f"  color: {theme.TEXT};"
            f"  border: none;"
            f"  border-radius: 2px;"
            f"  text-align: left;"
            f"  padding: 0 8px 0 {theme.TRIANGLE_TEXT_LEFT}px;"
            f"  font-family: '{theme.FAMILY_UI}';"
            f"  font-size: {theme.BASE_PT}pt;"
            f"  font-weight: {theme.WEIGHT_UI};"
            f"}}"
            f"QPushButton:hover {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
        )
        self._header_arrow = QLabel(self._header)
        self._header_arrow.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._header_arrow.setAlignment(Qt.AlignCenter)
        self._header_arrow.setGeometry(
            theme.TRIANGLE_LEFT, 0, theme.TRIANGLE_WIDTH, _HEADER_H
        )
        self._header_arrow.setStyleSheet(
            f"background: transparent; color: {theme.TRIANGLE}; border: none;"
            f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
        )
        self._header.clicked.connect(self._toggle)
        # Keep an eye on header resizes so any attached subbar can be
        # positioned inside the header without affecting layout metrics.
        try:
            self._header.installEventFilter(self)
        except Exception:
            pass
        self._refresh_header()
        header_row_layout.addWidget(self._header, 1)
        outer.addWidget(self._header_row)

        # Structural separator band between title and body content.
        # This band should be transparent so the TabPanel background shows
        # through as a continuous surface; keep the fixed height for the
        # canonical inspector gap.
        self._body_band = QWidget()
        self._body_band.setFixedHeight(theme.INSPECTOR_GAP)
        self._body_band.setStyleSheet("background: transparent;")
        self._body_band.setVisible(self._expanded)
        outer.addWidget(self._body_band)

        # ── Content area ─────────────────────────────────────────────────
        self._body = QWidget()
        # Do not paint the section body; it should be transparent so the
        # TabPanel background shows through as one continuous surface.
        self._body.setStyleSheet("background: transparent;")
        self._body.setVisible(self._expanded)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(theme.INSPECTOR_GAP)
        outer.addWidget(self._body)

    # ------------------------------------------------------------------ private

    def _refresh_header(self) -> None:
        arrow = "▼" if self._expanded else "▶"
        self._header_arrow.setText(arrow)
        subtitle = getattr(self, "_subtitle", "")
        if subtitle:
            self._header.setText(f"{self._title}: {subtitle}")
        else:
            self._header.setText(self._title)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh_header()
        self._body_band.setVisible(self._expanded)
        self._body.setVisible(self._expanded)
        # Subbars attached via `set_subbar()` are intended to remain
        # visible on the section header even when the body is collapsed.
        if self._subbar is not None:
            if self._subbar_persistent:
                self._subbar.setVisible(True)
            else:
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
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    # ------------------------------------------------------------------ public

    def add_widget(self, widget: QWidget) -> None:
        """Append *widget* to the content area."""
        self._body_layout.addWidget(widget)

    def set_subbar(self, widget: QWidget) -> None:
        """Insert *widget* between the header and the body.

        The widget is placed in the outer layout (not inside the collapsible
        body) so it remains visible even when the section is collapsed.
        Intended for the accent color loading bar so it shows during loading
        regardless of whether the Filter section is open or closed.
        """
        # Make the subbar a child of the header so it does not add any
        # vertical layout space. Position it at the bottom edge of the
        # header and keep it visible even when the section body is closed.
        self._subbar = widget
        self._subbar_persistent = True
        try:
            widget.setParent(self._header)
            widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            widget.setVisible(True)
            widget.raise_()
            # Position immediately; header resize events will keep it in place.
            try:
                h = self._header.height()
                sh = widget.height()
                widget.setGeometry(0, max(0, h - sh), max(1, self._header.width()), sh)
            except Exception:
                pass
        except Exception:
            # Fallback to the previous behaviour if anything goes wrong.
            widget.setParent(self)
            widget.setVisible(self._expanded)
            try:
                self.layout().insertWidget(1, widget)
            except Exception:
                pass

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

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # Keep an attached subbar positioned at the bottom of the header
        # when the header is resized. This avoids adding layout vertical
        # space while keeping the animation visually attached to the title.
        try:
            if obj is self._header and event.type() == QEvent.Resize:
                if self._subbar is not None and self._subbar.parent() is self._header:
                    try:
                        h = self._header.height()
                        sh = self._subbar.height()
                        self._subbar.setGeometry(0, max(0, h - sh), max(1, self._header.width()), sh)
                    except Exception:
                        pass
        except Exception:
            pass
        return super().eventFilter(obj, event)
