"""InspectorValue — canonical representation of a value inside an Inspector.

Part of the shared Visualizer Framework, at the same level as
`WindowVisualizer`, `Inspector`, `TabbedPanel`, `Tab`, `TabPanel`,
`CollapsibleSection`, and `MetadataBlock`::

    WindowVisualizer
        -> Inspector
        -> TabbedPanel -> Tab -> TabPanel
        -> CollapsibleSection
        -> MetadataBlock
        -> InspectorValue

`MetadataBlock` (and any other section that displays key/value rows)
constructs `InspectorValue` for its value cells instead of a raw `QLabel`.

InspectorValue represents the semantic concept of "a value displayed inside
an Inspector" — not "a QLabel". The implementation happens to inherit from
QLabel today, but callers should treat that as an implementation detail and
stick to the methods documented here (plus ordinary QLabel/QWidget methods
that are not value-presentation specific, e.g. `setStyleSheet()`).

Version 1 responsibilities (presentation only)
-----------------------------------------------
- wrapping, including character-level wrapping for unbroken strings
  (filenames, ids, hashes, …) that contain no legal Qt word-break point
- size policy / heightForWidth participation
- minimum-width negotiation decoupled from unbroken content length
- text selection
- tooltip support

Version 1 explicitly does NOT know about movies, filenames, identifiers, or
any other domain concept — formatting decisions like clipping, ellipsis,
or filename-specific truncation stay with the caller. Future versions may
add a `mode=` parameter (e.g. ``InspectorValue(mode="identifier")``,
``InspectorValue(mode="paragraph")``, ``InspectorValue(mode="filename")``)
to opt into such domain-aware presentation without visualizer code having
to change; Version 1 does not implement any mode.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QLabel, QSizePolicy, QWidget


def _wrap_anywhere(text: str) -> str:
    """Interleave *text* with zero-width spaces (U+200B) between characters.

    QLabel's word-wrap only breaks at legal boundaries (space, hyphen, …).
    Filesystem-derived strings (filenames, ids) are frequently joined with
    underscores/dots and contain no such boundary, so an unbroken run
    forces a `minimumSizeHint()` wide enough to fit the whole string on one
    line — which in turn forces the containing Inspector wider than
    intended. Interleaving a zero-width space after every character gives
    Qt's text layout a legal break point everywhere, without changing what
    is visibly rendered. This also collapses `minimumSizeHint()` down to
    roughly one character's width instead of the full string's width,
    which is what decouples an InspectorValue's minimum width from its
    content length.
    """
    return "\u200b".join(text)


class InspectorValue(QLabel):
    """A value displayed inside an Inspector — not "a QLabel".

    Usage::

        value = InspectorValue("Django (1966)")
        layout.addWidget(value)
        value.setText("Django Unchained (2012)")

    Callers should not rely on QLabel-specific behaviour beyond what is
    documented here; the base class may change in a future version.
    """

    #: Emitted at the end of setText(), after the wrap has been applied,
    #: whenever this value's displayed content changes. Containers (e.g.
    #: MetadataBlock) connect to this to recompute row geometry from the
    #: *current* content — this fires regardless of whether the caller went
    #: through a container's own API (e.g. MetadataBlock.set()) or holds a
    #: direct reference to this widget (e.g. via MetadataBlock.labels()), so
    #: row-height recomputation can never be silently skipped based on which
    #: path a caller used to change the text.
    valueChanged = pyqtSignal()

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        size_policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        size_policy.setHeightForWidth(True)
        self.setSizePolicy(size_policy)
        if text:
            self.setText(text)

    def setText(self, text: str) -> None:  # type: ignore[override]
        """Set the displayed value, with automatic character-level wrapping.

        Behaves like `QLabel.setText()` from the caller's perspective — the
        zero-width-space interleaving (see `_wrap_anywhere`) is invisible in
        the rendered output and exists purely so long unbroken strings stay
        wrappable instead of forcing the Inspector wider.
        """
        super().setText(_wrap_anywhere(text) if text else text)
        self.valueChanged.emit()
