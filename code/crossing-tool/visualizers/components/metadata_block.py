"""MetadataBlock — reusable key-value presentation block.

A fixed row set updated via ``set(key, value)`` and reset via ``clear()``.
Used inside inspector sections to keep metadata presentation consistent across
visualizers.

Example::

    block = MetadataBlock(["label", "film", "frame", "confidence"])
    block.set("label", "horse")
    block.set("film", "Django (1966)")
    block.set("frame", "1275")
    block.set("confidence", "0.91")
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QGridLayout, QLabel, QSizePolicy, QWidget

from styles import theme


class MetadataBlock(QWidget):
    """Fixed key-value row grid for inspector presentation.

    This widget presents metadata only. It does not own selection, data
    retrieval, or project operations.

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
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(0)
        layout.setVerticalSpacing(0)
        layout.setColumnStretch(0, 0)
        layout.setColumnStretch(1, 1)

        self._rows = list(rows)
        self._labels: dict[str, QLabel] = {}
        last_idx = len(self._rows) - 1
        for row_idx, key in enumerate(self._rows):
            top = f" border-top: 2px solid {theme.TAB_BG};" if row_idx == 0 else ""
            bottom = f" border-bottom: 2px solid {theme.TAB_BG};" if row_idx < last_idx else ""

            key_lbl = QLabel(key)
            key_lbl.setStyleSheet(
                f"background: {theme.CELL_BG}; color: {theme.TEXT_DIM};"
                f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
                f" font-weight: {theme.WEIGHT_UI};"
                f"{top} border-right: 2px solid {theme.TAB_BG}; {bottom}"
                " min-height: 24px; padding: 0px 4px 0px 2px;"
            )
            key_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            layout.addWidget(key_lbl, row_idx, 0)

            val_lbl = QLabel("—")
            val_lbl.setStyleSheet(
                f"background: {theme.CELL_BG}; color: {theme.TEXT};"
                f" font-family: '{theme.FAMILY_MONO}';"
                f" font-size: {theme.BASE_PT}pt;"
                f" font-weight: {theme.WEIGHT_MONO};"
                f"{top}{bottom} padding: 0px 2px 0px 3px;"
            )
            val_lbl.setWordWrap(True)
            val_lbl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            val_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            sp.setHeightForWidth(True)
            val_lbl.setSizePolicy(sp)
            layout.addWidget(val_lbl, row_idx, 1)

            self._labels[key] = val_lbl

    def labels(self) -> dict[str, QLabel]:
        """Return the mutable key → value-label mapping used by callers."""
        return dict(self._labels)

    def set(self, key: str, value: str) -> None:
        """Set the displayed value for *key*.  No-op if *key* is unknown."""
        if key in self._labels:
            self._labels[key].setText(value)

    def clear(self) -> None:
        """Reset all rows to the placeholder dash (—)."""
        for lbl in self._labels.values():
            lbl.setText("—")
