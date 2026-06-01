#!/usr/bin/env python3
"""Book Visualizer — browse imported books as page spreads.

Launched via:
    crossing visualizer book

Layout:
  LEFT  — open-book spread view (one spread at a time) + fuchsia page bar
  RIGHT — panel: book selector, page info, Import / New buttons

Spread layout (book-style):
  - Page 1 is displayed alone on the RIGHT side (front cover)
  - Pages 2-3, 4-5 … shown as left+right pairs filling available width
  - Last page alone on the LEFT side if total page count is even (back cover)

Navigation:
  Left/Right arrows — previous/next spread
  Click page bar    — jump to spread
  Home/End          — previous/next book
  Escape / Ctrl+Q / Ctrl+W — close
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme
from styles.theme import GripSplitter, save_window_geometry, restore_window_geometry

from PyQt5.QtCore import Qt, QEvent, pyqtSignal, QRect, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtGui import QImage, QPixmap, QPainter, QColor

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GUTTER       = 16    # px gap between left and right page in a spread
_MARGIN       = 24    # px outer margin around pages
_BAR_H        = 8     # px height of the page position bar
_PANEL_WIDTH  = 220   # px — right panel preferred width

_PANEL_STYLESHEET = (
    f"QWidget {{ background: {theme.PANEL_BG}; }}"
    f" QPushButton {{ background-color: {theme.BTN_BG}; border: none;"
    f" padding: 0 10px; border-radius: 3px;"
    f" min-height: {theme.BTN_H}px; max-height: {theme.BTN_H}px; }}"
    f" QPushButton:hover    {{ background-color: {theme.BTN_HOVER}; }}"
    f" QPushButton:pressed  {{ background-color: {theme.BTN_PRESSED}; }}"
    f" QPushButton:disabled {{ color: {theme.TEXT_DIM};"
    f" background-color: {theme.BTN_BG}; }}"
)


# ---------------------------------------------------------------------------
# Spread index helpers
# ---------------------------------------------------------------------------

def _spread_count(page_count: int) -> int:
    """Number of spreads for a book with *page_count* pages.

    Spread 0 = front cover (page 0 alone on right)
    Spread k = pages (2k-1, 2k) for k >= 1
    Last spread may be a single back-cover page (alone on left) when
    page_count is even.
    """
    if page_count <= 0:
        return 0
    # cover (1) + pairs for remaining pages (ceil)
    return 1 + (page_count - 1 + 1) // 2


def _pages_for_spread(spread_idx: int, page_count: int):
    """Return (left_page, right_page) 0-based indices for a spread.

    A missing page is represented as *None*.
    Spread 0: left=None, right=0  (front cover on right)
    Spread k (k>=1): left=2k-1, right=2k  (or right=None for back cover)
    """
    if spread_idx == 0:
        return None, 0
    left = 2 * spread_idx - 1
    right = 2 * spread_idx
    # back-cover case: even page_count means last spread is single page on left
    if right >= page_count:
        right = None
    return left, right


# ---------------------------------------------------------------------------
# _SpreadView — single-spread display widget
# ---------------------------------------------------------------------------

class _SpreadView(QWidget):
    """Displays one open-book spread.

    Renders only the currently visible spread directly from the open fitz
    document at the current widget size.  A short debounce timer prevents
    thrashing during window resize.  Results are cached by
    (slug, left_i, right_i, w, h).
    """

    _DEBOUNCE_MS = 100

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._doc   = None            # open fitz.Document
        self._slug  = ""
        self._left_i:  Optional[int] = None
        self._right_i: Optional[int] = None
        # cache: (slug, left_i, right_i, w, h) → (QImage|None, QImage|None)
        self._cache: dict = {}
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self._DEBOUNCE_MS)
        self._debounce.timeout.connect(self._do_render)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------

    def set_spread(
        self,
        doc,
        slug: str,
        left_i: Optional[int],
        right_i: Optional[int],
    ) -> None:
        """Set document + page indices then render immediately."""
        self._doc    = doc
        self._slug   = slug
        self._left_i  = left_i
        self._right_i = right_i
        self._debounce.stop()
        self._do_render()

    def clear(self) -> None:
        self._doc    = None
        self._slug   = ""
        self._left_i  = None
        self._right_i = None
        self._debounce.stop()
        self.update()

    def invalidate(self, slug: Optional[str] = None) -> None:
        """Drop cached renders (for one slug, or all)."""
        if slug is None:
            self._cache.clear()
        else:
            for k in list(self._cache):
                if k[0] == slug:
                    del self._cache[k]

    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._doc is not None:
            self._debounce.start()   # restart on each resize step; fires once idle

    # ------------------------------------------------------------------

    def _cell_size(self):
        """Return (page_w, avail_h) for a single page cell."""
        page_w = max(1, (self.width()  - 2 * _MARGIN - _GUTTER) // 2)
        avail_h = max(1,  self.height() - 2 * _MARGIN)
        return page_w, avail_h

    def _render_page(self, page_idx: int, cell_w: int, cell_h: int) -> Optional[QImage]:
        """Render one PDF page to fit cell_w × cell_h; returns QImage."""
        import fitz
        try:
            page = self._doc[page_idx]
        except Exception:
            return None
        rect = page.rect
        iw, ih = rect.width, rect.height
        if iw <= 0 or ih <= 0:
            return None
        scale = min(cell_w / iw, cell_h / ih)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return QImage(
            pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888
        ).copy()

    def _do_render(self) -> None:
        """Render current spread into cache then repaint."""
        if self._doc is None:
            self.update()
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        key = (self._slug, self._left_i, self._right_i, w, h)
        if key not in self._cache:
            cell_w, cell_h = self._cell_size()
            left_img  = self._render_page(self._left_i,  cell_w, cell_h) if self._left_i  is not None else None
            right_img = self._render_page(self._right_i, cell_w, cell_h) if self._right_i is not None else None
            self._cache[key] = (left_img, right_img)
            # Keep cache bounded
            while len(self._cache) > 10:
                del self._cache[next(iter(self._cache))]
        self.update()

    # ------------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.CANVAS_BG))

        if self._doc is None:
            p.end()
            return

        key = (self._slug, self._left_i, self._right_i, self.width(), self.height())
        if key not in self._cache:
            p.end()
            return

        left_img, right_img = self._cache[key]
        if left_img is None and right_img is None:
            p.end()
            return

        cell_w, cell_h = self._cell_size()
        m, g = _MARGIN, _GUTTER

        def _blit(img: QImage, x: int, align_right: bool) -> None:
            iw, ih = img.width(), img.height()
            dy = m + (cell_h - ih) // 2
            dx = (x + cell_w - iw) if align_right else x
            p.drawImage(dx, dy, img)

        if left_img is not None and right_img is not None:
            _blit(left_img,  m,              align_right=True)
            _blit(right_img, m + cell_w + g, align_right=False)
        elif right_img is not None:
            _blit(right_img, m + cell_w + g, align_right=False)
        elif left_img is not None:
            _blit(left_img,  m,              align_right=True)

        p.end()


# ---------------------------------------------------------------------------
# _PageBar — horizontal fuchsia position indicator
# ---------------------------------------------------------------------------

class _PageBar(QWidget):
    """Horizontal bar showing current spread position; clickable to jump."""

    jumped = pyqtSignal(int)   # emits spread index

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._spread_count = 0
        self._current = 0
        self.setFixedHeight(_BAR_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_state(self, current: int, total: int) -> None:
        self._current = current
        self._spread_count = total
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.INPUT_BG))
        n = self._spread_count
        if n > 0:
            cell_w = self.width() / n
            x = int(self._current * cell_w)
            pw = max(4, int(cell_w))
            p.fillRect(x, 0, pw, self.height(), QColor(theme.ACCENT))
        p.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._jump(event.x())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.LeftButton:
            self._jump(event.x())

    def _jump(self, x: int) -> None:
        if self._spread_count <= 0 or self.width() <= 0:
            return
        idx = int(x / self.width() * self._spread_count)
        idx = max(0, min(self._spread_count - 1, idx))
        self.jumped.emit(idx)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class BookVisualizerWindow(QMainWindow):
    """Main window for the Book Visualizer."""

    def __init__(self, project_path: str) -> None:
        super().__init__()
        self.setWindowTitle("Crossing — Book Visualizer")
        self._project_path = project_path

        self._books: list[dict] = []
        self._current_book_idx: int = 0   # index into self._books
        self._doc = None                  # open fitz.Document (or None)
        self._slug: str = ""              # slug of the currently open book
        self._spread_idx: int = 0         # current spread index
        self._updating_combo: bool = False

        self._build_ui()
        self._load_all_books()
        restore_window_geometry(self, "window_book")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._close_doc()
        save_window_geometry(self, "window_book")
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(0)

        splitter = GripSplitter(Qt.Horizontal)
        outer.addWidget(splitter, stretch=1)

        # ── LEFT: spread view + page bar ─────────────────────────────
        left_col = QWidget()
        left_col.setStyleSheet(f"background: {theme.CANVAS_BG};")
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        self._spread_view = _SpreadView()
        left_layout.addWidget(self._spread_view, stretch=1)

        self._page_bar = _PageBar()
        self._page_bar.jumped.connect(self._go_spread)
        left_layout.addWidget(self._page_bar)

        splitter.addWidget(left_col)

        # ── RIGHT: control panel (mosaic-style) ───────────────────────
        panel = self._build_control_panel()
        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        self.setMinimumSize(700, 480)
        self.resize(1200, 800)

    def _build_control_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(_PANEL_WIDTH)
        panel.setStyleSheet(_PANEL_STYLESHEET)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        # ── Book group ────────────────────────────────────────────────
        book_group = QGroupBox("Book")
        book_layout = QVBoxLayout(book_group)
        book_layout.setContentsMargins(8, 12, 8, 8)
        book_layout.setSpacing(6)

        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._combo.setStyleSheet(
            f"QComboBox {{ background: {theme.INPUT_BG}; }}"
        )
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        self._combo.installEventFilter(self)
        book_layout.addWidget(self._combo)

        layout.addWidget(book_group)

        # ── Info group ────────────────────────────────────────────────
        info_group = QGroupBox("Info")
        info_layout = QVBoxLayout(info_group)
        info_layout.setContentsMargins(8, 12, 8, 8)
        info_layout.setSpacing(4)

        self._pages_label = QLabel("—")
        self._pages_label.setWordWrap(True)
        self._pages_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"
        )
        info_layout.addWidget(self._pages_label)

        self._spread_label = QLabel("")
        self._spread_label.setWordWrap(True)
        self._spread_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {theme.BASE_PT}pt;"
        )
        info_layout.addWidget(self._spread_label)

        self._loading_label = QLabel("")
        self._loading_label.setWordWrap(True)
        self._loading_label.setStyleSheet(
            f"color: {theme.TEXT}; background: {theme.ACCENT};"
            f" font-size: {theme.BASE_PT}pt; padding: 3px 6px; border-radius: 3px;"
        )
        self._loading_label.hide()
        info_layout.addWidget(self._loading_label)

        layout.addWidget(info_group)

        # ── Actions group ─────────────────────────────────────────────
        actions_group = QGroupBox("Actions")
        actions_layout = QVBoxLayout(actions_group)
        actions_layout.setContentsMargins(8, 12, 8, 8)
        actions_layout.setSpacing(6)

        btn_grid = QGridLayout()
        btn_grid.setSpacing(4)
        btn_grid.setContentsMargins(4, 4, 4, 4)

        self._import_btn = QPushButton("Import")
        self._import_btn.setToolTip("Import a PDF into this book")
        self._import_btn.clicked.connect(self._on_import)
        btn_grid.addWidget(self._import_btn, 0, 0)

        self._new_btn = QPushButton("New")
        self._new_btn.setToolTip("Create a new book")
        self._new_btn.clicked.connect(self._on_new_book)
        btn_grid.addWidget(self._new_btn, 0, 1)

        btn_container = QFrame()
        btn_container.setStyleSheet(
            f"QFrame {{ background: {theme.INPUT_BG}; border-radius: 3px; }}"
        )
        btn_container.setLayout(btn_grid)
        actions_layout.addWidget(btn_container)

        layout.addWidget(actions_group)
        layout.addStretch()

        return panel

    # ------------------------------------------------------------------
    # Data loading

    def _load_all_books(self) -> None:
        """Populate the combo box and show the current book (or first)."""
        from data.book import list_books

        self._books = list_books(self._project_path)

        self._updating_combo = True
        self._combo.clear()
        for book in self._books:
            self._combo.addItem(book.get("slug", ""))
        self._updating_combo = False

        if not self._books:
            self._pages_label.setText("No books yet.")
            self._spread_view.clear()
            return

        try:
            from tool import prefs
            current = prefs.get("current_book")
        except Exception:
            current = None

        start_idx = 0
        if current:
            for i, book in enumerate(self._books):
                if book.get("slug") == current:
                    start_idx = i
                    break

        self._show_book(start_idx)

    def _show_book(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._books):
            return

        self._current_book_idx = idx
        self._close_doc()
        self._spread_idx = 0

        book = self._books[idx]
        slug = book.get("slug", "")
        pdf_rel = book.get("pdf")
        page_count = book.get("page_count", 0)

        if not self._updating_combo and self._combo.currentIndex() != idx:
            self._updating_combo = True
            self._combo.setCurrentIndex(idx)
            self._updating_combo = False

        self._page_bar.set_state(0, 0)
        self._spread_label.setText("")
        self._loading_label.hide()

        if not pdf_rel:
            self._pages_label.setText("No PDF imported.")
            self._import_btn.setEnabled(True)
            self._persist_current(slug)
            return

        from data.book import book_dir
        pdf_path = book_dir(self._project_path, slug) / pdf_rel

        if not pdf_path.exists():
            self._pages_label.setText("PDF file missing.")
            self._import_btn.setEnabled(True)
            self._persist_current(slug)
            return

        import fitz
        try:
            self._doc  = fitz.open(str(pdf_path))
            self._slug = slug
        except Exception as exc:
            self._pages_label.setText(f"Error: {exc}")
            return

        self._pages_label.setText(f"{page_count} pages")
        self._import_btn.setEnabled(False)
        self._persist_current(slug)
        self._show_spread()

    def _go_spread(self, idx: int) -> None:
        """Jump to spread *idx*."""
        if self._doc is None:
            return
        n = _spread_count(self._doc.page_count)
        if n == 0:
            return
        self._spread_idx = max(0, min(n - 1, idx))
        self._show_spread()

    def _show_spread(self) -> None:
        """Display self._spread_idx from the open document."""
        if self._doc is None:
            self._spread_view.clear()
            self._page_bar.set_state(0, 0)
            self._spread_label.setText("")
            return
        page_count = self._doc.page_count
        n = _spread_count(page_count)
        if n == 0:
            self._spread_view.clear()
            self._page_bar.set_state(0, 0)
            self._spread_label.setText("")
            return
        left_i, right_i = _pages_for_spread(self._spread_idx, page_count)
        self._spread_view.set_spread(self._doc, self._slug, left_i, right_i)
        self._page_bar.set_state(self._spread_idx, n)
        if left_i is None:
            page_str = f"p. 1 of {page_count}"
        elif right_i is None:
            page_str = f"p. {left_i + 1} of {page_count}"
        else:
            page_str = f"pp. {left_i + 1}–{right_i + 1} of {page_count}"
        self._spread_label.setText(page_str)

    def _close_doc(self) -> None:
        """Close the open fitz document and reset the spread view."""
        self._spread_view.clear()
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
            self._doc = None
        self._slug = ""

    def _persist_current(self, slug: str) -> None:
        try:
            from tool import prefs
            prefs.set("current_book", slug)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Button actions

    def _on_import(self) -> None:
        """Open a file dialog to import a PDF into the current book."""
        if not self._books:
            QMessageBox.warning(self, "No Book", "Create a book first.")
            return

        pdf_path, _ = QFileDialog.getOpenFileName(
            self, "Import PDF", "", "PDF Files (*.pdf)"
        )
        if not pdf_path:
            return

        book = self._books[self._current_book_idx]
        slug = book.get("slug", "")
        force = book.get("pdf") is not None  # overwrite if already imported

        from data.book import import_pdf
        try:
            data = import_pdf(self._project_path, slug, pdf_path, force=force)
        except Exception as exc:
            QMessageBox.critical(self, "Import Failed", str(exc))
            return

        # Refresh book list entry and redisplay
        self._books[self._current_book_idx] = data
        self._updating_combo = True
        self._combo.setItemText(self._current_book_idx, data.get("slug", slug))
        self._updating_combo = False
        self._import_btn.setEnabled(True)   # temporarily re-enable so _show_book can disable
        self._show_book(self._current_book_idx)

    def _on_new_book(self) -> None:
        """Prompt for a slug and create a new book."""
        slug, ok = QInputDialog.getText(
            self, "New Book", "Book slug (e.g. west-of-everything):"
        )
        if not ok or not slug.strip():
            return
        slug = slug.strip()

        from data.book import create_book
        try:
            data = create_book(self._project_path, slug)
        except FileExistsError as exc:
            QMessageBox.warning(self, "Already Exists", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))
            return

        self._books.append(data)
        self._updating_combo = True
        self._combo.addItem(slug)
        self._updating_combo = False

        new_idx = len(self._books) - 1
        self._show_book(new_idx)

    # ------------------------------------------------------------------
    # Event handling

    def _on_combo_changed(self, idx: int) -> None:
        if self._updating_combo:
            return
        self._show_book(idx)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._combo and event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Home:
                self._show_book(max(0, self._current_book_idx - 1))
                return True
            if key == Qt.Key_End:
                self._show_book(min(len(self._books) - 1, self._current_book_idx + 1))
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key  = event.key()
        mods = event.modifiers()

        if key == Qt.Key_Escape:
            self.close()
            return
        if key in (Qt.Key_Q, Qt.Key_W) and mods == Qt.ControlModifier:
            self.close()
            return
        if key == Qt.Key_Left:
            self._go_spread(self._spread_idx - 1)
            return
        if key == Qt.Key_Right:
            self._go_spread(self._spread_idx + 1)
            return
        if key == Qt.Key_Home:
            self._show_book(max(0, self._current_book_idx - 1))
            return
        if key == Qt.Key_End:
            self._show_book(min(len(self._books) - 1, self._current_book_idx + 1))
            return

        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_visualizer(project_path: str) -> None:
    """Launch the Book Visualizer window."""
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    window = BookVisualizerWindow(project_path)
    window.show()
    app.exec_()

