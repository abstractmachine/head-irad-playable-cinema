"""Lightweight window shell shared by modern visualizers.

WindowVisualizer owns only the application window and its canonical
shell: Browser | [side panel] | GripSplitter | Inspector. It implements
geometry persistence (including fullscreen state and inspector-panel
visibility), keyboard shortcuts (Tab, Shift+Tab, Esc, Ctrl+Q/W), and small
helper hooks for subclasses to provide the actual browser and inspector
widgets via `create_browser()` and `create_inspector()`.

A subclass may optionally provide a third, independent splitter pane
between the Browser and the Inspector via `create_side_panel()` — for
non-inspector "browser/tableau" content such as Book Visualizer's
Engravings catalog (see `visualizers.components.side_panel.SidePanel`).
This pane owns its own splitter/collapse behavior, uncoupled from the
Inspector's; it must never be nested inside the Inspector itself. When a
subclass does not override `create_side_panel()` (the default), the shell
behaves exactly like the classic two-pane Browser | Inspector layout.

Whatever `create_inspector()` returns may opt into shared scrollbar-gutter
reservation (the inspector pane widens/narrows by `theme.SCROLLBAR_W` as
its content's vertical scrollbar appears/disappears, so neighbouring
panes shift left and snap back) by setting a `gutter_tab_host` attribute
on itself, pointing at whichever `TabbedPanel` drives the active tab's
content — see `visualizers.components.scrollbar_gutter`. `Inspector`
does this automatically; a visualizer that owns a `TabbedPanel` directly
without wrapping it in an `Inspector` (e.g. Book Visualizer) can set this
attribute itself to participate in the same canonical behavior.

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

        # Last known windowed (non-fullscreen) geometry, kept up to date via
        # resizeEvent()/moveEvent() below. Tracked separately from Qt's own
        # normalGeometry() (which is not always reliable across window
        # managers) so it can be persisted even if this window is closed
        # while fullscreen — see closeEvent() / save_window_geometry().
        self._normal_geometry = None

        # Set by restore_window_geometry() in __init__ below when this
        # window should start fullscreen. Consumed by show() so the window
        # goes straight to fullscreen on first display instead of briefly
        # appearing as a normal (or maximized-looking) window first — that
        # intermediate state is what let some window managers ignore the
        # follow-up fullscreen request.
        self._pending_fullscreen = False

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

        self._side_panel = self.create_side_panel()

        self._inspector_shell = self.create_inspector()
        if self._inspector_shell is None:
            self._inspector_shell = QWidget()

        self._splitter.addWidget(self._browser)
        if self._side_panel is not None:
            self._splitter.addWidget(self._side_panel)
        self._splitter.addWidget(self._inspector_shell)

        self._splitter.setStretchFactor(0, 1)
        for i in range(1, self._splitter.count()):
            self._splitter.setStretchFactor(i, 0)
        try:
            # handle may not exist in tests/mocks; guard defensively
            # The Inspector is always the last pane, regardless of whether an
            # optional side panel is present.
            self._splitter.handle(self._splitter.count() - 1).installEventFilter(self)
        except Exception:
            pass

        # normal startup (no debug instrumentation)

        layout.addWidget(self._splitter)

        # Attach shared scrollbar-gutter behavior when the inspector shell
        # exposes a "tab host" to drive it (see
        # visualizers.components.scrollbar_gutter). `Inspector` sets this
        # attribute on itself, but any widget returned by create_inspector()
        # may set `gutter_tab_host` to whichever TabbedPanel it owns
        # directly — this is what lets a non-Inspector inspector shell
        # (e.g. Book Visualizer's control panel) participate in the same
        # canonical gutter-reservation behavior as Metadata/Illustration/
        # Project without wrapping its content in an Inspector.
        tab_host = getattr(self._inspector_shell, "gutter_tab_host", None)
        if tab_host is not None:
            try:
                from visualizers.components.scrollbar_gutter import attach_scrollbar_gutter
                attach_scrollbar_gutter(
                    tab_host, self._splitter, pane_index=self._splitter.count() - 1
                )
            except Exception:
                pass

        QTimer.singleShot(0, self._fit_splitter_width)

        if self._pref_key:
            panel_hidden, want_fullscreen = restore_window_geometry(self, self._pref_key)
            self._pending_fullscreen = want_fullscreen
            if panel_hidden:
                # Schedule after the _fit_splitter_width() call above (same
                # singleShot(0) queue, so it runs right after) — otherwise
                # that call's unconditional setMinimumWidth(inspector_w)
                # would immediately undo the hide performed here.
                QTimer.singleShot(0, self._toggle_inspector)

    # Subclass hooks -------------------------------------------------
    def create_browser(self) -> Optional[QWidget]:
        raise NotImplementedError()

    def create_side_panel(self) -> Optional[QWidget]:
        """Optional hook: an independent, non-inspector splitter pane placed
        between the Browser and the Inspector (e.g. a browser/tableau side
        panel such as Book Visualizer's Engravings catalog).

        Returns None by default — no extra pane is added, and the shell
        behaves exactly like the classic two-pane Browser | Inspector
        layout. A subclass that needs one should typically wrap its content
        in `visualizers.components.side_panel.SidePanel` so it can be shown
        and fully hidden (not just collapsed to zero width) independent of
        the Inspector.
        """
        return None

    def create_inspector(self) -> Optional[QWidget]:
        raise NotImplementedError()

    # Focus -----------------------------------------------------------
    def focus_target(self) -> QWidget:
        """Widget that should hold keyboard focus whenever this window is
        shown. Defaults to the window itself, which is what every
        keyPressEvent-based shortcut (Tab/Shift+Tab/Esc here, plus each
        visualizer's own arrow-key/letter shortcuts) needs in order to
        reliably receive key presses. Without this, whichever child widget
        Qt's default focus algorithm happens to pick first (e.g. a toolbar
        button) can silently swallow key presses instead — this is what
        broke Book Visualizer's LEFT/RIGHT page navigation previously.
        Override this in a subclass that needs a specific child widget
        (e.g. a drawing canvas) to hold focus instead.
        """
        return self

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.focus_target().setFocus()

    def show(self) -> None:
        # If restore_window_geometry() (see __init__) determined this window
        # should start fullscreen, go there directly instead of showing
        # normal first and upgrading to fullscreen a moment later — some
        # window managers ignore (or briefly show as merely maximized) a
        # fullscreen request that follows too closely after an initial
        # normal show(), which is what caused windows to reopen looking
        # maximized instead of truly fullscreen.
        if self._pending_fullscreen:
            self._pending_fullscreen = False
            self.showFullScreen()
        else:
            super().show()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._capture_normal_geometry()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._capture_normal_geometry()

    def _capture_normal_geometry(self) -> None:
        """Cache the current windowed geometry for later persistence.

        Clamps width/height to the current screen's available size first.
        Some window-manager/DPI-scaling glitches can transiently report a
        geometry far larger than any real screen; caching that unchecked
        would later get written out via closeEvent()/save_window_geometry()
        and reproduced on the next launch, leaving the window effectively
        stuck (too large to see its own edges to resize back down).
        """
        if self.isFullScreen():
            return
        geo = self.geometry()
        try:
            from PyQt5.QtWidgets import QApplication
            screen = QApplication.primaryScreen().availableGeometry()
            w = max(200, min(geo.width(),  screen.width()))
            h = max(150, min(geo.height(), screen.height()))
            if w != geo.width() or h != geo.height():
                geo.setWidth(w)
                geo.setHeight(h)
        except Exception:
            pass
        self._normal_geometry = geo

    # Geometry -------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self._pref_key:
            normal_geometry = None
            if self.isFullScreen() and self._normal_geometry is not None:
                r = self._normal_geometry
                normal_geometry = (r.x(), r.y(), r.width(), r.height())
            save_window_geometry(
                self, self._pref_key,
                panel_hidden=self._inspector_hidden,
                normal_geometry=normal_geometry,
            )
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
                # Capture the windowed geometry right before entering
                # fullscreen so it's available to save even if this window
                # is later closed (or quits) while still fullscreen —
                # resizeEvent()/moveEvent() normally keep this up to date,
                # but capturing it explicitly here removes any doubt.
                self._normal_geometry = self.geometry()
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

        if self._side_panel is not None and self._side_panel.isVisible():
            try:
                side_w = max(1, self._side_panel.sizeHint().width())
            except Exception:
                side_w = 1
            browser_w = max(1, total - inspector_w - side_w)
            self._splitter.setSizes([browser_w, side_w, inspector_w])
        else:
            browser_w = max(1, total - inspector_w)
            sizes = [browser_w, inspector_w]
            if self._side_panel is not None:
                sizes = [browser_w, 0, inspector_w]
            self._splitter.setSizes(sizes)

    def _sync_inspector_min_width(self) -> None:
        if self._inspector_hidden:
            self._inspector_shell.setMinimumWidth(0)
            return
        sizes = self._splitter.sizes()
        if not sizes:
            return
        # The Inspector is always the last pane, whether or not an optional
        # side panel is present.
        inspector_w = max(0, sizes[-1])
        self._inspector_shell.setMinimumWidth(inspector_w)

    def _toggle_inspector(self) -> None:
        if self._inspector_hidden:
            self._inspector_shell.setVisible(True)
            self._inspector_hidden = False
            if self._saved_splitter_sizes and len(self._saved_splitter_sizes) == self._splitter.count():
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
        # Ensure splitter handle clicks keep min width in sync. The
        # Inspector's handle is always the last one, whether or not an
        # optional side panel pane is present.
        try:
            inspector_handle = self._splitter.handle(self._splitter.count() - 1)
            if obj is inspector_handle and event.type() == QEvent.MouseButtonRelease:
                self._inspector_shell.setMinimumWidth(0)
                QTimer.singleShot(0, self._sync_inspector_min_width)
        except Exception:
            pass
        return super().eventFilter(obj, event)
