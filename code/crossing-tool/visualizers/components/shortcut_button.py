"""ShortcutButton — action button with its key hint flush right.

Factored out of ``project_visualizer.py``'s ``_LauncherButton`` so the
F-key launcher buttons and any other visualizer's shortcut hints share one
implementation. Project Visualizer subclasses this; behaviour there is
unchanged.

The name and hint are child ``QLabel``s laid out with a stretch (Qt's
horizontal spacer) so the hint sits against the right edge regardless of name
length — a single padded string does not line up across buttons of differing
name width. Because they are child labels they do not pick up
``theme.action_button_stylesheet()``'s hover/pressed/checked/disabled colours
the way plain button text would, so those states are mirrored manually here.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton

from styles import theme

SPACER_ROW_INSET = 8
SPACER_ROW_GAP = 6


class ShortcutButton(QPushButton):
    """A button labelled ``name`` with ``shortcut`` hinted at its right edge."""

    def __init__(self, label: str, shortcut: str | None = None, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACER_ROW_INSET, 0, SPACER_ROW_INSET, 0)
        layout.setSpacing(SPACER_ROW_GAP)

        self._name_label = QLabel(label)
        self._name_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._name_label.setFont(theme.font_ui(bold=True))
        layout.addWidget(self._name_label)

        layout.addStretch(1)

        self._shortcut_label = None
        if shortcut:
            self._shortcut_label = QLabel(shortcut)
            self._shortcut_label.setAttribute(Qt.WA_TransparentForMouseEvents)
            self._shortcut_label.setFont(theme.font_ui(bold=False))
            layout.addWidget(self._shortcut_label)

        self._apply_colors()

    def text(self) -> str:
        """The visible name, since the label lives in a child widget."""
        return self._name_label.text()

    def setText(self, text: str) -> None:  # noqa: N802
        self._name_label.setText(text)

    def shortcut_text(self) -> str:
        return self._shortcut_label.text() if self._shortcut_label else ""

    def _apply_colors(self) -> None:
        if not self.isEnabled():
            color = dim_color = "rgba(255,255,255,0.15)"
        elif self.isDown():
            color = dim_color = theme.ACCENT
        elif self.isChecked() or self.underMouse():
            # Painted on ACCENT, so both take the paired accent foreground.
            color = dim_color = theme.ACCENT_TEXT
        else:
            color = theme.TEXT
            dim_color = theme.SHORTCUT_TEXT
        self._name_label.setStyleSheet(f"color: {color}; background: transparent;")
        if self._shortcut_label is not None:
            self._shortcut_label.setStyleSheet(
                f"color: {dim_color}; background: transparent;")

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        super().setEnabled(enabled)
        self._apply_colors()

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        super().setChecked(checked)
        self._apply_colors()

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self._apply_colors()

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self._apply_colors()
