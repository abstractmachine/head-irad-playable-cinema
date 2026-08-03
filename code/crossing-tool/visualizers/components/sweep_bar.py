"""SweepBar — shared 2px accent-stripe loading indicator.

A thin, borderless loading indicator: a fixed-width ACCENT-coloured stripe
sweeping left → right. Uses a custom ``paintEvent`` so no track is ever
drawn — the idle state is completely invisible and the active colour matches
``theme.ACCENT`` exactly, unaffected by any style engine.

Designed to be attached to a `CollapsibleSection` header via
``CollapsibleSection.set_subbar()`` so the animation stays visible on the
section title even while the section body is collapsed — this is the
canonical way to signal "this section's data is still loading" throughout
the app.

Usage::

    self._loading_bar = SweepBar(self)
    self._loading_timer = QTimer(self)
    self._loading_timer.setInterval(20)   # ~50 fps
    self._loading_timer.timeout.connect(self._loading_bar.tick)
    section.set_subbar(self._loading_bar)

    # When loading starts/stops:
    self._loading_bar.start(); self._loading_timer.start()
    self._loading_timer.stop(); self._loading_bar.stop()
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QWidget

from styles import theme


class SweepBar(QWidget):
    """2 px loading indicator: a fixed-width ACCENT stripe sweeping left → right."""

    _STRIPE_RATIO = 0.30   # stripe is 30 % of bar width
    _STEP         = 0.04   # advance per tick (~50 fps → ~500 ms per sweep)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(2)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._t      = 0.0
        self._active = False

    def start(self) -> None:
        self._t      = 0.0
        self._active = True
        self.update()

    def stop(self) -> None:
        self._active = False
        self.update()

    def tick(self) -> None:
        if self._active:
            # t runs from 0 → 1+STRIPE_RATIO so the stripe fully exits on the right
            self._t += self._STEP
            if self._t > 1.0 + self._STRIPE_RATIO:
                self._t = -self._STRIPE_RATIO        # restart fully off-screen left
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(0, 0, 0, 0))   # transparent background
        if self._active and w > 0:
            sw  = max(1, int(w * self._STRIPE_RATIO))
            # leading edge of the stripe
            x = int(self._t * w) - sw
            x1 = max(0, x)
            x2 = min(w, x + sw)
            if x2 > x1:
                p.fillRect(x1, 0, x2 - x1, h, QColor(theme.ACCENT))
        p.end()
