"""MetadataBlock — compact key-value metadata display widget.

A fixed set of labelled rows that can be updated individually via
``set(key, value)`` and cleared via ``clear()``.  Used as a building block
inside IllustrationInspector panels.

Example::

    block = MetadataBlock(["label", "film", "frame", "confidence"])
    block.set("label", "horse")
    block.set("film", "Django (1966)")
    block.set("frame", "1275")
    block.set("confidence", "0.91")
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from styles import theme


class MetadataBlock(QWidget):
    """Fixed grid of key–value label pairs for inspector panels.

    Parameters
    ----------
    rows:
        Ordered list of key names.  Each key gets one row: a right-aligned
        dim label on the left and a white value label on the right.
    parent:
        Optional parent widget.
    """

    def __init__(self, rows: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._labels: dict[str, QLabel] = {}
        for key in rows:
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(4)

            key_lbl = QLabel(f"{key}:")
            key_lbl.setStyleSheet(
                f"color: {theme.TEXT_DIM};"
                f" font-family: '{theme.FAMILY_MONO}';"
                f" font-size: {theme.BASE_PT}pt;"
            )
            key_lbl.setFixedWidth(72)
            key_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row_layout.addWidget(key_lbl)

            val_lbl = QLabel("—")
            val_lbl.setStyleSheet(
                f"color: {theme.TEXT};"
                f" font-family: '{theme.FAMILY_MONO}';"
                f" font-size: {theme.BASE_PT}pt;"
            )
            val_lbl.setWordWrap(True)
            row_layout.addWidget(val_lbl, 1)

            layout.addWidget(row_widget)
            self._labels[key] = val_lbl

    def set(self, key: str, value: str) -> None:
        """Set the displayed value for *key*.  No-op if *key* is unknown."""
        if key in self._labels:
            self._labels[key].setText(value)

    def clear(self) -> None:
        """Reset all rows to the placeholder dash (—)."""
        for lbl in self._labels.values():
            lbl.setText("—")
