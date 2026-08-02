"""SidePanel — independent splitter pane for non-inspector browser/tableau
content (e.g. Book Visualizer's Engravings catalog; future Shot/Scene
tableaux).

`SidePanel` is deliberately *not* part of the Inspector/TabbedPanel
hierarchy. Some panels are neither tabs nor inspector content — they are
browser-adjacent surfaces (a thumbnail catalog, a scene/shot tableau) whose
visibility is driven by something else in the window (e.g. a workspace tab
elsewhere), not by the Inspector's own show/hide state.

A `SidePanel` must be added directly to the window's outer `GripSplitter` as
a sibling of the Inspector (typically via `WindowVisualizer.create_side_panel()`)
rather than nested inside it. That way its own grip handle collapses/expands
only this panel — never the Inspector alongside it — for free, via the
standard `GripSplitter` click-to-collapse-the-right-pane convention.

`SidePanel` itself only owns one thing beyond plain content hosting: clean
show/hide. `set_active(False, splitter)` fully removes the panel *and its
adjacent grip handle* from the splitter's layout flow (`setVisible(False)`),
rather than merely collapsing it to zero width, so a hidden panel never
leaves behind reserved/centered space. `set_active(True, splitter)` restores
the panel's last known width, borrowed from the nearest resizable neighbour.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QSize
from PyQt5.QtWidgets import QSplitter, QVBoxLayout, QWidget


class SidePanel(QWidget):
    """A splitter pane wrapping *content* with clean hide/show semantics.

    `preferred_width` is used both as the initial `sizeHint()` width and as
    the fallback restored width the first time the panel is shown.
    """

    def __init__(
        self,
        content: QWidget,
        preferred_width: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._preferred_width = int(preferred_width)
        self._last_width = int(preferred_width)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(content)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._preferred_width, super().sizeHint().height())

    def set_active(self, active: bool, splitter: Optional[QSplitter] = None) -> None:
        """Show or fully hide this panel within *splitter*.

        When *active* is False the panel is hidden — Qt also hides its
        adjacent grip handle and reclaims its space, so nothing is left
        reserved. When *active* is True the panel is shown again and its
        previous width (or `preferred_width`, the first time) is restored by
        borrowing space from the nearest resizable neighbour.
        """
        sp = splitter if splitter is not None else self.parentWidget()

        if not active:
            if self.isVisible() and sp is not None:
                try:
                    idx = sp.indexOf(self)
                    sizes = sp.sizes()
                    if 0 <= idx < len(sizes) and sizes[idx] > 0:
                        self._last_width = sizes[idx]
                except Exception:
                    pass
            self.setVisible(False)
            return

        self.setVisible(True)
        if sp is None:
            return
        try:
            idx = sp.indexOf(self)
            sizes = list(sp.sizes())
        except Exception:
            return
        if idx < 0 or idx >= len(sizes):
            return

        target = max(1, self._last_width or self._preferred_width)
        # Borrow the needed width from the nearest resizable neighbour,
        # preferring the pane to the left (e.g. the canvas/browser).
        donor = idx - 1 if idx > 0 else idx + 1
        if donor < 0 or donor >= len(sizes):
            return
        take = min(target, max(0, sizes[donor] - 1))
        sizes[donor] -= take
        sizes[idx] = take
        sp.setSizes(sizes)
