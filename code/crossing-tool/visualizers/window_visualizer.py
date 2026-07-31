"""Lightweight window shell shared by modern visualizers.

WindowVisualizer owns only the application window and its canonical
three-pane shell: Browser | GripSplitter | Inspector.  It implements
geometry persistence, keyboard shortcuts (Tab, Shift+Tab, Esc, Ctrl+Q/W),
and small helper hooks for subclasses to provide the actual browser and
inspector widgets via `create_browser()` and `create_inspector()`.

Keep this file intentionally small and generic — no project- or media-
specific logic belongs here.
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, QEvent, QTimer
from PyQt5.QtWidgets import (
    QWidget, QHBoxLayout, QSizePolicy, QMainWindow,
)

from tool.shortcuts import VisualizerWindow
from styles.theme import GripSplitter, save_window_geometry, restore_window_geometry


class WindowVisualizer(VisualizerWindow):
    """Very small reusable window shell.

    Subclasses must implement `create_browser()` and `create_inspector()`.
    """

    def __init__(self, pref_key: Optional[str] = None) -> None:
        super().__init__()
        self._pref_key = pref_key

        self._inspector_hidden = False
        self._saved_splitter_sizes: list[int] = []

        root = QWidget()
        self.setCentralWidget(root)
        root.setContentsMargins(0, 0, 0, 0)

        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._splitter = GripSplitter(Qt.Horizontal)

        # Delegate widget creation to subclass hooks
        self._browser = self.create_browser()
        if self._browser is None:
            self._browser = QWidget()
        self._browser.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._inspector_shell = self.create_inspector()
        if self._inspector_shell is None:
            self._inspector_shell = QWidget()

        self._splitter.addWidget(self._browser)
        self._splitter.addWidget(self._inspector_shell)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        try:
            # handle may not exist in tests/mocks; guard defensively
            self._splitter.handle(1).installEventFilter(self)
        except Exception:
            pass

        # normal startup (no debug instrumentation)

        layout.addWidget(self._splitter)

        # Attach shared inspector scrollbar gutter behavior when possible.
        try:
            # Import locally to avoid module-level circular imports.
            from visualizers.components.inspector import Inspector
            inspector_obj = None
            if isinstance(self._inspector_shell, Inspector):
                inspector_obj = self._inspector_shell
            else:
                # Adapter wrappers (e.g. _TabWidgetCompat) expose the
                # underlying Inspector on `_inspector` — use that when present.
                nested = getattr(self._inspector_shell, "_inspector", None)
                if isinstance(nested, Inspector):
                    inspector_obj = nested
            if inspector_obj is not None and hasattr(inspector_obj, "attach_scrollbar_gutter"):
                inspector_obj.attach_scrollbar_gutter(self._splitter, inspector_index=1)
        except Exception:
            pass

        QTimer.singleShot(0, self._fit_splitter_width)

        if self._pref_key:
            restore_window_geometry(self, self._pref_key)

    # Subclass hooks -------------------------------------------------
    def create_browser(self) -> Optional[QWidget]:
        raise NotImplementedError()

    def create_inspector(self) -> Optional[QWidget]:
        raise NotImplementedError()

    # Geometry -------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self._pref_key:
            save_window_geometry(self, self._pref_key)
        super().closeEvent(event)

    # Shortcuts / behavior ------------------------------------------
    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()
        if key == Qt.Key_Escape:
            self.close()
            return
        if key in (Qt.Key_Q, Qt.Key_W) and mod & Qt.ControlModifier:
            self.close()
            return
        if key in (Qt.Key_Backtab, Qt.Key_Tab) and mod & Qt.ShiftModifier and not (
            mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)
        ):
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            return
        if key == Qt.Key_Tab and not (
            mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier | Qt.ShiftModifier)
        ):
            self._toggle_inspector()
            return
        super().keyPressEvent(event)

    # Debug helper removed
    # Splitter layout helpers ---------------------------------------
    def _fit_splitter_width(self) -> None:
        total = self._splitter.width()
        if total <= 0:
            QTimer.singleShot(100, self._fit_splitter_width)
            return
        try:
            inspector_w = max(320, self._inspector_shell.sizeHint().width())
        except Exception:
            inspector_w = 320
        self._inspector_shell.setMinimumWidth(inspector_w)
        self._splitter.setSizes([max(1, total - inspector_w), inspector_w])

    def _sync_inspector_min_width(self) -> None:
        if self._inspector_hidden:
            self._inspector_shell.setMinimumWidth(0)
            return
        sizes = self._splitter.sizes()
        if len(sizes) != 2:
            return
        inspector_w = max(0, sizes[1])
        self._inspector_shell.setMinimumWidth(inspector_w)

    def _toggle_inspector(self) -> None:
        if self._inspector_hidden:
            self._inspector_shell.setVisible(True)
            self._inspector_hidden = False
            if self._saved_splitter_sizes and len(self._saved_splitter_sizes) == 2:
                self._splitter.setSizes(self._saved_splitter_sizes)
            else:
                QTimer.singleShot(0, self._fit_splitter_width)
            QTimer.singleShot(0, self._sync_inspector_min_width)
            return

        self._saved_splitter_sizes = list(self._splitter.sizes())
        self._inspector_shell.setVisible(False)
        self._inspector_hidden = True
        self._inspector_shell.setMinimumWidth(0)

    def eventFilter(self, obj, event) -> bool:
        # Ensure splitter handle clicks keep min width in sync
        try:
            if obj is self._splitter.handle(1) and event.type() == QEvent.MouseButtonRelease:
                self._inspector_shell.setMinimumWidth(0)
                QTimer.singleShot(0, self._sync_inspector_min_width)
        except Exception:
            pass
        return super().eventFilter(obj, event)
