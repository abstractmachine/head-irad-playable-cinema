"""TabbedPanel / Tab — shared tab architecture for visualizer inspectors.

Introduces the missing piece of the framework hierarchy::

    WindowVisualizer -> Inspector -> TabbedPanel -> Tab -> TabPanel / ...

`TabbedPanel` owns a themed tab bar (`theme.tab_strip_stylesheet()`) and
switches between pages. Unlike `QTabWidget`/`QStackedWidget`, only the
active page is ever parented/mounted — hidden pages are fully detached,
so they can never influence `sizeHint()`/`minimumSizeHint()`, scrollbar
visibility, or splitter/gutter sizing. (Qt's built-in stacked containers
size for the largest *registered* page even while it is hidden — that is
exactly the bug this component exists to avoid; see the now-removed
`IllustrationPane._SinglePageHost` / `_TabWidgetCompat` this generalizes.)

`Tab` is the single-page container contract handed to
`TabbedPanel.add_tab()`. It owns nothing but a zero-margin layout around
one content widget — typically a `TabPanel` (the scrollable
CollapsibleSection shell), but any widget is accepted so simple pages
are not forced to allocate a TabPanel they don't need.

Only `setCurrentIndex` / `currentIndex` / `currentChanged` use Qt's
camelCase naming — this mirrors `QTabWidget`'s own API so existing call
sites written against a real `QTabWidget` (or the previous ad hoc
`_TabWidgetCompat` adapter) can switch to `TabbedPanel` with minimal
changes. Every other method follows the snake_case convention used by
the rest of the shared components (`TabPanel.add_widget`,
`CollapsibleSection.add_widget`, ...).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QTabBar, QVBoxLayout, QWidget

from styles import theme


class Tab(QWidget):
    """A single page container for `TabbedPanel`.

    Wraps one content widget (commonly a `TabPanel`) with no margins or
    spacing of its own. `Tab` does not know about scrolling, sections, or
    styling — that is the content widget's responsibility.
    """

    def __init__(self, content: Optional[QWidget] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._content: Optional[QWidget] = None
        if content is not None:
            self.set_content(content)

    def set_content(self, widget: QWidget) -> None:
        """Replace this page's content widget."""
        if self._content is not None:
            self._layout.removeWidget(self._content)
            self._content.setParent(None)
        self._content = widget
        self._layout.addWidget(widget)

    def content(self) -> Optional[QWidget]:
        return self._content


class _TabPageHost(QWidget):
    """Mounts exactly one `Tab` at a time.

    Registered pages that are not current stay fully unparented (not just
    hidden) so they never contribute to this host's — or any ancestor's —
    sizeHint/minimumSizeHint, and a hidden page's internal scrollbar can
    never affect layout.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._pages: Dict[int, Tab] = {}
        self._current_index = -1

    def register(self, index: int, page: Tab) -> None:
        self._pages[index] = page

    def set_current(self, index: int) -> None:
        if index == self._current_index:
            return
        current = self._pages.get(self._current_index)
        if current is not None:
            self._layout.removeWidget(current)
            current.setParent(None)
            current.hide()

        page = self._pages.get(index)
        self._current_index = index if page is not None else -1
        if page is not None:
            page.setParent(self)
            self._layout.addWidget(page)
            page.show()

    def current_index(self) -> int:
        return self._current_index


class TabbedPanel(QWidget):
    """Shared tab bar + single-active-page host.

    Composition contract:
      - A themed `QTabBar` pinned at the top (`theme.tab_strip_stylesheet()`).
      - A page host beneath it that mounts only the active `Tab`.

    `TabbedPanel` owns tab switching only. It knows nothing about what a
    page contains — visualizers compose each page (typically a `TabPanel`
    with `CollapsibleSection`s) and hand it to `add_tab()`.
    """

    currentChanged = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._tab_bar = QTabBar(self)
        self._tab_bar.setExpanding(False)
        self._tab_bar.setUsesScrollButtons(False)
        self._tab_bar.setDrawBase(False)
        self._tab_bar.setFocusPolicy(Qt.NoFocus)
        self._tab_bar.setStyleSheet(theme.tab_strip_stylesheet())
        self._tab_bar.currentChanged.connect(self._on_bar_changed)

        self._host = _TabPageHost(self)

        outer.addWidget(self._tab_bar)
        outer.addWidget(self._host)

        self._pages: List[Tab] = []

    # ------------------------------------------------------------------ build
    def add_tab(self, content: QWidget, title: str) -> Tab:
        """Add *content* as a new page titled *title*; return its `Tab`.

        Mirrors `QTabWidget.addTab(widget, label)` argument order so
        call sites migrating off a raw `QTabWidget` need minimal changes.
        The first tab added becomes the active page.
        """
        tab = Tab(content)
        index = len(self._pages)
        self._pages.append(tab)
        self._host.register(index, tab)
        self._tab_bar.addTab(title)
        if index == 0:
            self._host.set_current(0)
        return tab

    def tab_bar(self) -> QTabBar:
        """Return the underlying `QTabBar` for rare direct customization."""
        return self._tab_bar

    def count(self) -> int:
        return len(self._pages)

    def widget(self, index: int) -> Optional[QWidget]:
        """Return the content widget at *index* (`QTabWidget`-compatible)."""
        if 0 <= index < len(self._pages):
            return self._pages[index].content()
        return None

    def tab_page(self, index: int) -> Optional[Tab]:
        """Return the `Tab` wrapper at *index* (rarely needed directly)."""
        if 0 <= index < len(self._pages):
            return self._pages[index]
        return None

    # ------------------------------------------------------------ Qt-style API
    def currentIndex(self) -> int:
        return self._tab_bar.currentIndex()

    def setCurrentIndex(self, index: int) -> None:
        self._tab_bar.setCurrentIndex(index)

    def currentWidget(self) -> Optional[QWidget]:
        return self.widget(self.currentIndex())

    def _on_bar_changed(self, index: int) -> None:
        self._host.set_current(index)
        self.currentChanged.emit(index)
