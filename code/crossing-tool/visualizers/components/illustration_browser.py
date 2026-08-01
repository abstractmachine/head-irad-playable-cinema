"""IllustrationBrowser — canonical collection-browsing surface.

The browser is the reusable visualization surface for illustration collections.
It owns browsing mechanics only: loading, filtering, pagination, keyboard
navigation, drag-and-drop payloads, and selection.

The browser does not own editing, generation, business logic, project logic,
or CLI operations. It never needs to know why the user is viewing records; it
only knows how to browse and select them.

Configured once at construction time.  Two built-in sources are supported:

    ``"silhouettes"``  — scans the silhouette catalog via
                         ``services.silhouette_catalog.scan_catalog``
    ``"engravings"``   — scans ``data/engravings/catalog/`` directly

Filter hierarchy
----------------
The browser uses a five-level cascade::

    Media   →  "movie" or "gameplay"
      ↓
    Item    →  specific film (filename_stem), or "-- all --"
      ↓
    Field   →  annotation field (objects, setting …), or "-- all --"
      ↓
    Letter  →  first-letter group (A–Z), or "-- all --"
      ↓
    Keyword →  specific label (horse, revolver …), or "-- all --"

Sources that have no field taxonomy (engravings) receive ``"--all"`` for
Field and the cascade degrades gracefully to a flat scope-filtered browser.

Selection
---------
Selection belongs exclusively to the browser. Everything that reacts to
selection (inspectors, toolbars, workflow controls) subscribes to
``selectionChanged`` rather than maintaining a second selection state.

Usage::

    browser = IllustrationBrowser(
        project_path="/path/to/project",
        source="silhouettes",
    )
    browser.selectionChanged.connect(inspector.setItem)
    browser.itemActivated.connect(window.on_insert_requested)
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QEvent, QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from styles import theme
from visualizers.components.illustration_source import IllustrationSource
from visualizers.components.thumbnail_cell import ThumbnailCell
from visualizers.components.thumbnail_loader import ThumbnailLoader
from visualizers.components.combo_popup import attach_combo_popup

# ---------------------------------------------------------------------------
# Combo popup border fix
# ---------------------------------------------------------------------------

class _ComboPopupFixer(QObject):
    """Event filter installed on a QComboBox's internal view.

    When the view is reparented into Qt's internal QComboBoxPrivateContainer
    (a QFrame that draws the popup border), this filter immediately removes
    that frame's border via setFrameStyle(0).  The event used is
    QEvent.ParentChange, which fires at exactly that moment.

    Setting styles on the *combo* widget or its *panel* parent cannot reach
    the popup container because it becomes a separate top-level window.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.ParentChange:
            container = obj.parentWidget()
            # The container is a QFrame subclass; 0 = QFrame.NoFrame.
            if container is not None:
                container.setFrameStyle(0)
        return False


# ---------------------------------------------------------------------------
# Background catalog loader
# ---------------------------------------------------------------------------

class _CatalogLoader(QThread):
    """Background worker: calls ``IllustrationSource._load()`` off the main
    thread so the UI stays responsive during a catalog scan.

    After loading the raw items it performs a single combined pass to extract
    all data needed to populate the four filter combos (films, fields, letters,
    label counts).  Both results are emitted together so the main thread never
    has to scan the full item list again — it just calls addItem().

    The caller is responsible for disconnecting stale loaders before starting
    a new one (see ``IllustrationBrowser._stop_catalog_loader``).
    """

    loaded = pyqtSignal()  # zero args: avoids PyQt5 copying 343K items across threads

    def __init__(
        self,
        source: IllustrationSource,
        media_type: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._source      = source
        self._media_type  = media_type
        self._cancelled   = False
        self.result_items: list = []
        self.result_cache: dict = {}

    def cancel(self) -> None:
        """Signal that the result should be discarded even if the scan
        finishes.  The underlying I/O cannot be interrupted."""
        self._cancelled = True

    @staticmethod
    def _build_filter_cache(items: list) -> dict:
        """Single O(n) pass extracting all combo data from *items*.

        Returns a dict with pre-sorted lists / dicts ready for addItem()
        calls so the main-thread cascade never needs to iterate the full
        item list again.

        Yields the GIL every 10 000 records so the main-thread animation
        stays smooth during the pass.
        """
        films: set[str]  = set()
        fields: set[str] = set()
        letters: set[str] = set()
        counts: dict[str, int] = {}
        for i, r in enumerate(items):
            stem = _clean_stem(r.get("filename_stem", ""))
            if stem:
                films.add(stem)
            fields.add(r.get("field") or "--all")
            lbl = r.get("label", "")
            if lbl:
                ch = lbl[0].upper()
                letters.add(ch if ch.isalpha() else "#")
                counts[lbl] = counts.get(lbl, 0) + 1
            # Yield the GIL every 10 000 items so the main thread can
            # repaint the progress-bar animation without stutter.
            if i % 10_000 == 9_999:
                time.sleep(0)
        has_hash = "#" in letters
        return {
            "films":    sorted(films, key=str.casefold),
            "fields":   fields,
            "letters":  (["#"] if has_hash else []) + sorted(letters - {"#"}, key=str.casefold),
            "counts":   counts,           # label → total count (unfiltered)
        }

    def run(self) -> None:
        # _load() is a pure read — no side-effects on the source object.
        items = self._source._load(self._media_type)
        if not self._cancelled:
            # Build the filter cache while still in the background thread.
            cache = self._build_filter_cache(items)
            # Store results on self so the main-thread slot can read them
            # without any cross-thread argument marshalling by PyQt5.
            self.result_items = items
            self.result_cache = cache
            self.loaded.emit()   # zero-arg signal — no data copied

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_THUMB_GAP = 6    # px gap between cells in the grid
_PAGE_SIZE  = 50  # default items per page
_THUMB_SIZE = 80  # default thumbnail cell dimension (px, square)

# Annotation field display order — matches the vocabulary used by the
# silhouette catalog and the existing CatalogBrowser.
_FIELD_ORDER: list[str] = [
    "--all", "setting", "description", "objects",
    "action", "humans", "wearing", "animals", "text",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_stem(stem: str) -> str:
    """Strip TMDb suffix and normalise whitespace in a filename stem."""
    return re.sub(r"\s*\{tmdb-\d+\}", "", stem).strip()

# ---------------------------------------------------------------------------
# IllustrationBrowser
# ---------------------------------------------------------------------------

class _SweepBar(QWidget):
    """2 px loading indicator: a fixed-width ACCENT stripe sweeping left → right.

    Uses a custom paintEvent so no QProgressBar track is ever drawn — the idle
    state is completely invisible and the active colour matches theme.ACCENT
    exactly, unaffected by any style engine.
    """
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

    def paintEvent(self, event) -> None:
        from PyQt5.QtGui import QPainter, QColor
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


class IllustrationBrowser(QWidget):
    """Canonical visualizer browser for collection navigation.

    Ownership
    ---------
    This class owns collection browsing state: active filters, current page,
    and current selection. It is the canonical left-side browsing surface for
    visualizers built on this framework.

    Non-responsibilities
    --------------------
    This class does not implement project operations, generation pipelines,
    or business decisions. Those remain in services and CLI commands; this
    widget only exposes browse/select interactions.

    Parameters
    ----------
    project_path:
        Absolute path to the Crossing project directory.
    source:
        ``"silhouettes"`` or ``"engravings"``.
    media_type:
        Initial media type: ``"movie"`` or ``"gameplay"``.
    thumb_size:
        Fixed pixel dimension for thumbnail cells (square).
    page_size:
        Maximum number of thumbnails shown per page.
    parent:
        Optional parent widget.
    """

    # ------------------------------------------------------------------
    selectionChanged = pyqtSignal(dict)
    """Emitted whenever the selected item changes.

    ``item`` is the selected record dict from the current source.
    Inspector and tool widgets observe this signal; they do not own selection.
    """

    itemActivated = pyqtSignal(dict)
    """Emitted on double-click or Enter — the user intends to use the item."""

    catalogReloaded = pyqtSignal()
    """Emitted after a background catalog scan completes and the grid is rebuilt."""

    keywordChanged = pyqtSignal(str)
    """Emitted whenever the active keyword filter changes.

    The payload is the selected keyword label (e.g. ``"horse"``), or an empty
    string when the keyword filter is cleared (``<Keyword>`` / no selection).
    """

    # ------------------------------------------------------------------ init

    _FIELD_ORDER = _FIELD_ORDER

    # ------------------------------------------------------------------
    def __init__(
        self,
        source: IllustrationSource,
        media_type: Optional[str] = None,
        thumb_size: int = _THUMB_SIZE,
        page_size:  int = _PAGE_SIZE,
        detach_controls: bool = False,
        light_bg: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        """Create an IllustrationBrowser.

        Parameters
        ----------
        detach_controls:
            When ``True`` the filter section, status bar, and pagination bar
            are *not* added to the browser's own layout.  They are instead
            exposed as ``self.filter_panel``, ``self.status_bar``, and
            ``self.pagination_panel`` so the caller can place them in a
            separate pane.  The browser widget itself then shows only the
            thumbnail grid.
        """
        super().__init__(parent)

        self._source           = source
        self._media_type       = media_type
        self._thumb_size       = thumb_size
        self._light_bg         = light_bg
        # _page_size is now a dynamic property — see @property below.
        # The constructor argument is kept for API compatibility but not stored.
        self._detach_controls  = detach_controls

        # All items for the current media_type — reloaded by reload()
        self._all_items: list[dict] = []

        # Filtered items — rebuilt by _apply_filters()
        self._filtered_items: list[dict] = []

        # Selection: index into _filtered_items, -1 = no selection
        self._selected_index: int = -1

        # Pagination
        self._page_index: int = 0

        # ThumbnailCell widgets for the current grid page
        self._cells: list[ThumbnailCell] = []
        # Actual column count last used when building the grid — used by
        # navigate_grid so arrow keys always match the visible layout.
        self._grid_cols: int = 1

        # Background thumbnail loader — cancelled before each grid rebuild
        self._loader: Optional[ThumbnailLoader] = None
        self._catalog_loader: Optional[_CatalogLoader] = None
        # Monotonic counter: incremented every time a new cascade starts.
        # Deferred steps compare against it so stale lambdas self-cancel.
        self._cascade_id: int = 0
        # Pre-computed filter data built by _CatalogLoader in the background.
        # Keys: "films", "fields", "letters", "counts".
        self._filter_cache: dict = {}
        # Debounce timer: fires after the user stops resizing the window.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._on_resize_settled)

        # Guard against recursive combo-signal handling
        self._updating: bool = False

        self._build_ui()
        # Defer first load until Qt finishes the initial layout pass
        QTimer.singleShot(0, self.reload)

    # ------------------------------------------------------------------ public API

    def reload(self) -> None:
        """Reload all items from the source for the current media type.

        When no media type is selected the browser clears immediately (no I/O).
        Otherwise a ``_CatalogLoader`` thread is started so the scan runs off
        the main thread — the UI stays responsive and an accent progress bar is
        shown until the results arrive.

        Data-access responsibility remains in the source. The browser triggers
        reloads but does not scan directories or parse metadata files itself.
        """
        self._stop_catalog_loader()
        # Always clear combos and grid immediately so the UI reflects the new
        # (possibly empty) state even before the background scan finishes.
        self._all_items      = []
        self._filter_cache   = {}  # invalidate stale cache
        self._selected_index = -1
        self._page_index     = 0
        self._rebuild_item_combo()

        if not self._media_type:
            self._loading_timer.stop()
            self._loading_bar.stop()
            return

        self._status_lbl.setText("")
        self._loading_bar.start()
        self._loading_timer.start()
        self._catalog_loader = _CatalogLoader(
            self._source, self._media_type, parent=self
        )
        self._catalog_loader.loaded.connect(self._on_catalog_loaded)
        self._catalog_loader.start()

    def _on_catalog_loaded(self) -> None:
        """Slot called on the main thread when the background scan finishes."""
        loader = self._catalog_loader
        items  = loader.result_items
        cache  = loader.result_cache
        self._loading_timer.stop()
        self._loading_bar.stop()
        self._stop_loader()          # cancel any stale thumbnail loader
        # Sync the source's internal record cache so that refresh() and
        # set_sort_keys() (called by sort controls) see the loaded data.
        # _CatalogLoader._load() is a pure read and does NOT update _records,
        # so we must push the result back here.
        # Allow sources to post-process freshly loaded records (e.g. apply an
        # in-memory mode filter) before _all_items is updated.
        if hasattr(self._source, "_on_records_loaded"):
            self._source._on_records_loaded(items)
            items = self._source.items()  # get post-processed items
        else:
            self._source._records = list(items)
        self._all_items      = items
        self._filter_cache   = cache  # pre-computed in background thread
        self._selected_index = -1
        self._page_index     = 0
        # Run the combo cascade directly (no extra timer hop — saves the
        # ~2.5 s gap we observed between preamble and _rebuild_item_combo).
        self._rebuild_item_combo()
        self.catalogReloaded.emit()
        pass  # timer fires directly on _loading_bar.tick via connect

    def _stop_catalog_loader(self) -> None:
        """Disconnect and cancel any in-flight catalog load."""
        if self._catalog_loader is not None:
            try:
                self._catalog_loader.loaded.disconnect(self._on_catalog_loaded)
            except Exception:
                pass
            if self._catalog_loader.isRunning():
                self._catalog_loader.cancel()
                self._catalog_loader.wait(500)
            self._catalog_loader = None
        # _rebuild_item_combo cascades through the full filter hierarchy and
        # calls _apply_filters() at the end — no explicit call needed here.

    def currentItem(self) -> Optional[dict]:
        """Return the browser-owned selected record, or ``None``.

        Inspector and action panels should read selection through this API,
        not by storing duplicate copies of selected state.
        """
        if 0 <= self._selected_index < len(self._filtered_items):
            return self._filtered_items[self._selected_index]
        return None

    # ------------------------------------------------------------------ keyboard navigation (public helpers)

    def stepItem(self, delta: int) -> None:
        """Advance the Item (film scope) combo by *delta* steps."""
        self._step_combo(self._item_combo, delta)

    def stepField(self, delta: int) -> None:
        """Advance the Field combo by *delta* steps."""
        self._step_combo(self._field_combo, delta)

    def stepLetter(self, delta: int) -> None:
        """Advance the Letter combo by *delta* steps."""
        self._step_combo(self._letter_combo, delta)

    def stepKeyword(self, delta: int) -> None:
        """Advance the Keyword combo by *delta* steps."""
        self._step_combo(self._keyword_combo, delta)

    def refresh(self) -> None:
        """Re-apply filters from the source's cached data without reloading from disk.

        Use after an in-memory source change (e.g. sort-order update) that does
        not require a full catalog scan.  Preserves the current filter combo
        selections where the values still exist in the new data.

        This method refreshes browser presentation state only. It does not
        perform project operations or mutate source-of-truth metadata.
        """
        self._all_items      = self._source.items()
        self._selected_index = -1
        self._page_index     = 0
        self._rebuild_item_combo()

    def navigate_direct(
        self,
        item: Optional[str] = None,
        keyword: Optional[str] = None,
        object_id: Optional[str] = None,
    ) -> None:
        """Fast navigation to a known item+keyword, bypassing the cascade.

        Unlike ``navigate_to_filters``, this method does NOT rebuild the
        field / letter / keyword combos step-by-step.  It applies the filter
        in a single O(n) pass and rebuilds the grid immediately — typically
        <50 ms even on a 400 k-item catalog.

        ``object_id`` is the stem of the source JSON (e.g. ``"object_0001"``).
        When given, the matching cell in the grid is selected after the grid
        is built.
        """
        # ── set item combo without triggering cascade ──────────────────────
        if item is not None:
            self._item_combo.blockSignals(True)
            found = False
            for i in range(self._item_combo.count()):
                if self._item_combo.itemData(i) == item:
                    self._item_combo.setCurrentIndex(i)
                    found = True
                    break
            self._item_combo.blockSignals(False)

        # ── resolve real keyword from the actual record when object_id given ─
        # The engraving label is the directory name under the film — the
        # normalised form of the silhouette's original label string.  We need
        # the *real* label (from the silhouette JSON) so the filter matches.
        # object_id is only unique *within* a label directory, so we must also
        # match the parent directory name (= engraving label) to avoid picking
        # the wrong object_0001 from a different label in the same film.
        if object_id and (keyword is None or keyword):
            for r in self._all_items:
                sil_path = Path(str(r.get("path", "")))
                if (_clean_stem(r.get("filename_stem", "")) == (item or "") and
                        sil_path.stem == object_id and
                        sil_path.parent.name == (keyword or "")):
                    keyword = r.get("label") or keyword
                    break

        # ── reset field / letter to "--all" (no cascade) ──────────────────
        for _combo in (self._field_combo, self._letter_combo):
            _combo.blockSignals(True)
            for i in range(_combo.count()):
                if _combo.itemData(i) == "--all":
                    _combo.setCurrentIndex(i)
                    break
            _combo.blockSignals(False)

        # ── single-pass filter ─────────────────────────────────────────────
        scope = self._item_combo.currentData()
        records = self._all_items
        if scope:
            records = [r for r in records
                       if _clean_stem(r.get("filename_stem", "")) == scope]
        if keyword:
            records = [r for r in records if r.get("label", "") == keyword]

        self._filtered_items = records
        self._selected_index = -1
        self._page_index     = 0

        # ── update keyword combo to reflect the selection ──────────────────
        self._keyword_combo.blockSignals(True)
        self._keyword_combo.clear()
        self._keyword_combo.addItem("<Keyword>", userData="--all")
        if keyword:
            self._keyword_combo.addItem(
                f"{keyword}  ({len(records)})", userData=keyword
            )
            self._keyword_combo.setCurrentIndex(1)
        else:
            self._keyword_combo.setCurrentIndex(0)
        self._keyword_combo.blockSignals(False)
        # Emit currentIndexChanged so _refresh_color runs and the selected
        # keyword displays in TEXT colour (not the grey placeholder colour).
        self._keyword_combo.currentIndexChanged.emit(self._keyword_combo.currentIndex())

        # ── rebuild grid and emit keyword signal ───────────────────────────
        self.keywordChanged.emit(keyword or "")
        self._rebuild_grid()

        # ── select the specific item if object_id supplied ─────────────────
        if object_id:
            for abs_idx, r in enumerate(self._filtered_items):
                if Path(str(r.get("path", ""))).stem == object_id:
                    self.select_index(abs_idx)
                    break

    def navigate_to_filters(
        self,
        item: Optional[str] = None,
        field: Optional[str] = None,
        letter: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> None:
        """Set filter levels in cascade order (item → field → letter → keyword).

        When *item* is set alongside lower levels, the lower levels are deferred
        by 250 ms so the async combo-rebuild cascade triggered by changing the
        item combo has time to complete before we try to set keyword etc.
        """
        if item is not None:
            for i in range(self._item_combo.count()):
                if self._item_combo.itemData(i) == item:
                    self._item_combo.setCurrentIndex(i)
                    break
            if field is not None or letter is not None or keyword is not None:
                _f, _l, _kw = field, letter, keyword
                QTimer.singleShot(250, lambda: self.navigate_to_filters(
                    field=_f, letter=_l, keyword=_kw))
            return

        if field is not None:
            for i in range(self._field_combo.count()):
                if self._field_combo.itemData(i) == field:
                    self._field_combo.setCurrentIndex(i)
                    break
            if letter is not None or keyword is not None:
                _l, _kw = letter, keyword
                QTimer.singleShot(250, lambda: self.navigate_to_filters(
                    letter=_l, keyword=_kw))
            return

        if letter is not None:
            for i in range(self._letter_combo.count()):
                if self._letter_combo.itemData(i) == letter:
                    self._letter_combo.setCurrentIndex(i)
                    break
            if keyword is not None:
                _kw = keyword
                QTimer.singleShot(250, lambda: self.navigate_to_filters(keyword=_kw))
            return

        if keyword is not None:
            for i in range(self._keyword_combo.count()):
                if self._keyword_combo.itemData(i) == keyword:
                    self._keyword_combo.setCurrentIndex(i)
                    break

    def reset_filters(self) -> None:
        """Reset Title / Field / Letter / Keyword to placeholders.

        Forces the cascade even when the item combo is already at index 0
        (where setCurrentIndex(0) would be a no-op and fire no signal).
        The Media combo is intentionally left unchanged.
        """
        # Temporarily move to -1 so going back to 0 always fires
        # currentIndexChanged and triggers the full cascade rebuild.
        self._item_combo.blockSignals(True)
        self._item_combo.setCurrentIndex(-1)
        self._item_combo.blockSignals(False)
        self._item_combo.setCurrentIndex(0)   # fires signal → cascade

    def select_index(self, abs_idx: int) -> None:
        """Select the item at *abs_idx* in the current filtered list.

        Navigates to the correct page automatically and emits
        ``selectionChanged``.
        """
        if not (0 <= abs_idx < len(self._filtered_items)):
            return
        page = abs_idx // self._page_size
        if page != self._page_index:
            self._page_index = page
            self._rebuild_grid()
        page_idx = abs_idx % self._page_size
        self._on_cell_clicked(page_idx)

    def navigate_grid(self, delta_col: int, delta_row: int) -> None:
        """Move the grid selection by (*delta_col*, *delta_row*) cells.

        If nothing is selected, item 0 is selected regardless of direction.

        Left / right move linearly through the cell array and wrap naturally
        at row boundaries (hitting the right edge jumps to the first cell of
        the next row; hitting the left edge jumps to the last cell of the
        previous row).  Up / down move by a full row, staying in the same
        column.  All movement is clamped at the absolute first and last cell.
        """
        n = len(self._cells)
        if n == 0:
            return
        if self._selected_index < 0:
            self._on_cell_clicked(0)
            return
        cols     = max(1, self._grid_cols)
        page_idx = self._selected_index - self._page_index * self._page_size

        if delta_row != 0:
            # Up / down: jump one full row, stay in the same column
            row, col   = divmod(page_idx, cols)
            total_rows = (n - 1) // cols + 1
            new_row    = max(0, min(total_rows - 1, row + delta_row))
            new_idx    = min(new_row * cols + col, n - 1)
        else:
            # Left / right: move one step in reading order; wraps at row ends
            new_idx = max(0, min(n - 1, page_idx + delta_col))

        if new_idx != page_idx:
            self._on_cell_clicked(new_idx)
            if 0 <= new_idx < len(self._cells):
                self._scroll.ensureWidgetVisible(self._cells[new_idx])

    def refresh_highlights(self) -> None:
        """Update ``human_best`` highlight state on all visible cells.

        Lighter than a full ``refresh()`` — use after mark/unmark best
        operations where only the highlight border needs to change.
        """
        for i, cell in enumerate(self._cells):
            abs_idx = self._page_index * self._page_size + i
            if abs_idx < len(self._filtered_items):
                cell.set_highlighted(
                    bool(self._filtered_items[abs_idx].get("human_best"))
                )

    # ------------------------------------------------------------------ UI construction

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Always build all sections — they may live inside the browser or be
        # placed externally when detach_controls=True.
        self.filter_panel    = self._build_filter_section()
        self.status_bar      = self._build_status_bar()
        self.pagination_panel = self._build_pagination_bar()

        if self._detach_controls:
            # Pure-grid mode: only the thumbnail area is in the browser widget.
            outer.addWidget(self._build_grid_area(), 1)
        else:
            outer.addWidget(self.filter_panel)
            outer.addWidget(self.status_bar)
            outer.addWidget(self._build_grid_area(), 1)
            outer.addWidget(self.pagination_panel)

    def _build_filter_section(self) -> QWidget:
        widget = QWidget()
        # No explicit background — inherits from parent (CollapsibleSection body)
        # so it matches the Sort and Info sections visually.
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        combo_style = (
            f"QComboBox {{ background: {theme.BTN_BG}; color: {theme.TEXT};"
            f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
            f" font-weight: bold; border: none;"
            f" border-radius: 3px; padding: 0px 6px;"
            f" min-height: 24px; max-height: 24px; }}"
            f"QComboBox::drop-down {{ border: none; }}"
        )
        lbl_style = (
            f"color: {theme.TEXT_DIM}; font-family: '{theme.FAMILY_UI}';"
            f" font-size: {theme.BASE_PT}pt; font-weight: {theme.WEIGHT_UI}; background: transparent;"
        )

        def _combo(combo: QComboBox) -> None:
            def _refresh_color(_idx: int = 0, _c=combo) -> None:
                _col = theme.TEXT_DIM if _c.currentData() in (None, "--all") else theme.TEXT
                _c.setStyleSheet(
                    f"QComboBox {{ background: {theme.BTN_BG}; color: {_col};"
                    f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt;"
                    f" font-weight: {theme.WEIGHT_UI}; border: none;"
                    f" border-radius: 3px; padding: 0px 6px;"
                    f" min-height: 24px; max-height: 24px; }}"
                    f"QComboBox::drop-down {{ border: none; }}"
                )
            combo.currentIndexChanged.connect(_refresh_color)
            _refresh_color()
            combo.setFocusPolicy(Qt.NoFocus)
            combo.setMaxVisibleItems(6)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToContentsOnFirstShow)
            # Attach the canonical popup view and styling
            attach_combo_popup(combo)
            layout.addWidget(combo)

        # Media — <Media> means "nothing selected" (fast empty-browser start)
        self._media_combo = QComboBox()
        self._media_combo.addItem("<Media>", userData=None)
        self._media_combo.addItem("movie",    userData="movie")
        self._media_combo.addItem("gameplay", userData="gameplay")
        if self._media_type:
            idx = self._media_combo.findData(self._media_type)
            if idx >= 0:
                self._media_combo.setCurrentIndex(idx)
        self._media_combo.currentIndexChanged.connect(self._on_media_changed)
        _combo(self._media_combo)

        # Item (film scope)
        self._item_combo = QComboBox()
        self._item_combo.currentIndexChanged.connect(self._on_item_changed)
        _combo(self._item_combo)

        # Field
        self._field_combo = QComboBox()
        self._field_combo.currentIndexChanged.connect(self._on_field_changed)
        _combo(self._field_combo)

        # Letter
        self._letter_combo = QComboBox()
        self._letter_combo.currentIndexChanged.connect(self._on_letter_changed)
        _combo(self._letter_combo)

        # Keyword
        self._keyword_combo = QComboBox()
        self._keyword_combo.currentIndexChanged.connect(self._on_keyword_changed)
        _combo(self._keyword_combo)

        # Let the browser intercept navigation keys from combo dropdowns
        for combo in (
            self._item_combo, self._field_combo,
            self._letter_combo, self._keyword_combo,
        ):
            combo.installEventFilter(self)

        return widget

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Thin sweep bar — always in layout (2 px) so the height is constant.
        # A custom widget paints a fixed-width ACCENT stripe that sweeps left
        # to right.  No QProgressBar track is ever drawn, so the idle state is
        # truly invisible and the animation colour is exact regardless of style.
        self._loading_bar = _SweepBar(self)
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(20)   # ~50 fps
        self._loading_timer.timeout.connect(self._loading_bar.tick)
        # NOTE: loading bar is NOT added to this layout; the caller (CatalogBrowser)
        # inserts it into the Filter section header via set_subbar() so it is
        # visible even when the Filter section is collapsed.

        # Status label — kept as an attribute but NOT added to the layout;
        # 'No items' is now shown inside the pagination bar between the arrows.
        self._status_lbl = QLabel("")
        return bar

    def _build_grid_area(self) -> QScrollArea:
        self._scroll = QScrollArea()
        self._scroll.setVerticalScrollBar(theme.JumpScrollBar())
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {theme.CANVAS_BG}; border: none; }}"
        )

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet(f"background: {theme.CANVAS_BG};")
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setContentsMargins(
            _THUMB_GAP, _THUMB_GAP, _THUMB_GAP, _THUMB_GAP
        )
        self._grid_layout.setSpacing(_THUMB_GAP)
        self._grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self._scroll.setWidget(self._grid_widget)
        return self._scroll

    def _build_pagination_bar(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(4)

        # Use the shared action button stylesheet so pagination arrows use
        # the canonical highlight/hover/pressed/checked colors (ACCENT).
        _page_btn_style = theme.action_button_stylesheet()

        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedSize(24, 24)
        self._prev_btn.setFocusPolicy(Qt.NoFocus)
        self._prev_btn.setStyleSheet(_page_btn_style)
        self._prev_btn.clicked.connect(self._on_prev_page)
        lay.addWidget(self._prev_btn)

        self._page_lbl = QLabel("—")
        self._page_lbl.setAlignment(Qt.AlignCenter)
        self._page_lbl.setStyleSheet(
            f"color: {theme.TEXT}; font-family: '{theme.FAMILY_MONO}';"
            f" font-size: {theme.BASE_PT}pt; font-weight: {theme.WEIGHT_MONO};"
        )
        lay.addWidget(self._page_lbl, 1)

        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedSize(24, 24)
        self._next_btn.setFocusPolicy(Qt.NoFocus)
        self._next_btn.setStyleSheet(_page_btn_style)
        self._next_btn.clicked.connect(self._on_next_page)
        lay.addWidget(self._next_btn)

        return bar

    # ------------------------------------------------------------------ filter cascade

    def _on_media_changed(self, _idx: int) -> None:
        if self._updating:
            return
        new_type = self._media_combo.currentData()  # None, "movie", or "gameplay"
        if new_type != self._media_type:
            self._media_type = new_type
            self.reload()

    def _on_item_changed(self, _idx: int) -> None:
        if self._updating:
            return
        self._selected_index = -1
        self._page_index     = 0
        self._rebuild_field_combo()

    def _on_field_changed(self, _idx: int) -> None:
        if self._updating:
            return
        self._selected_index = -1
        self._page_index     = 0
        self._rebuild_letter_combo()

    def _on_letter_changed(self, _idx: int) -> None:
        if self._updating:
            return
        self._selected_index = -1
        self._page_index     = 0
        self._rebuild_keyword_combo()

    def _on_keyword_changed(self, _idx: int) -> None:
        if self._updating:
            return
        self._selected_index = -1
        self._page_index     = 0
        self._apply_filters()

    # ------------------------------------------------------------------ combo rebuilds
    # Each level rebuilds the levels below it, ultimately ending at
    # _apply_filters() → _rebuild_grid().
    #
    # The cascade is PROGRESSIVE: each function increments _cascade_id,
    # does its own work synchronously, then schedules the next level via
    # QTimer.singleShot(0, …) so the event loop can repaint / animate
    # between steps.  A deferred lambda is a no-op when _cascade_id has
    # changed (i.e. a newer interaction started a fresh cascade).

    def _rebuild_item_combo(self) -> None:
        """Rebuild the Item (film) combo from all loaded items."""
        self._cascade_id += 1
        _cid = self._cascade_id
        if self._filter_cache:
            films = self._filter_cache["films"]
        else:
            films = sorted(
                set(
                    _clean_stem(r.get("filename_stem", ""))
                    for r in self._all_items
                    if r.get("filename_stem")
                ),
                key=str.casefold,
            )
        prev = self._item_combo.currentData()
        self._item_combo.blockSignals(True)
        self._item_combo.clear()
        self._item_combo.addItem("<Title>", userData=None)
        for stem in films:
            self._item_combo.addItem(stem, userData=stem)
        idx = self._item_combo.findData(prev)
        self._item_combo.setCurrentIndex(max(0, idx))
        self._item_combo.blockSignals(False)
        self._item_combo.currentIndexChanged.emit(self._item_combo.currentIndex())
        QTimer.singleShot(0, lambda: self._rebuild_field_combo() if self._cascade_id == _cid else None)

    def _rebuild_field_combo(self) -> None:
        """Rebuild the Field combo for the current item scope."""
        self._cascade_id += 1
        _cid = self._cascade_id
        scope = self._item_combo.currentData()  # None → all
        if not scope and self._filter_cache:
            present = self._filter_cache["fields"]
        else:
            records = (
                [r for r in self._all_items
                 if _clean_stem(r.get("filename_stem", "")) == scope]
                if scope else self._all_items
            )
            present = set()
            for r in records:
                f = r.get("field") or "--all"
                present.add(f)

        prev = self._field_combo.currentData()
        self._field_combo.blockSignals(True)
        self._field_combo.clear()
        self._field_combo.addItem("<Field>", userData="--all")
        for f in self._FIELD_ORDER:
            if f != "--all" and f in present:
                self._field_combo.addItem(f, userData=f)
        for f in sorted(present - set(self._FIELD_ORDER) - {"--all"}):
            self._field_combo.addItem(f, userData=f)
        idx = self._field_combo.findData(prev)
        self._field_combo.setCurrentIndex(max(0, idx))
        self._field_combo.blockSignals(False)
        self._field_combo.currentIndexChanged.emit(self._field_combo.currentIndex())
        QTimer.singleShot(0, lambda: self._rebuild_letter_combo() if self._cascade_id == _cid else None)

    def _rebuild_letter_combo(self) -> None:
        """Rebuild the Letter combo for the current item+field scope."""
        self._cascade_id += 1
        _cid = self._cascade_id
        scope = self._item_combo.currentData()
        field = self._field_combo.currentData() or "--all"
        if not scope and field == "--all" and self._filter_cache:
            # Use pre-computed letters from the background pass
            letters = self._filter_cache["letters"]
        else:
            records = self._all_items
            if scope:
                records = [
                    r for r in records
                    if _clean_stem(r.get("filename_stem", "")) == scope
                ]
            if field != "--all":
                records = [
                    r for r in records
                    if (r.get("field") or "--all") == field
                ]
            # Group non-alphabetic label initials under '#'
            letters_set: set[str] = set()
            for r in records:
                lbl = r.get("label", "")
                if lbl:
                    ch = lbl[0].upper()
                    letters_set.add(ch if ch.isalpha() else "#")
            alpha_letters = sorted(letters_set - {"#"}, key=str.casefold)
            letters = (["#"] if "#" in letters_set else []) + alpha_letters
        prev = self._letter_combo.currentData()
        self._letter_combo.blockSignals(True)
        self._letter_combo.clear()
        self._letter_combo.addItem("<Letter>", userData="--all")
        for ch in letters:
            self._letter_combo.addItem(ch, userData=ch)
        idx = self._letter_combo.findData(prev)
        self._letter_combo.setCurrentIndex(max(0, idx))
        self._letter_combo.blockSignals(False)
        self._letter_combo.currentIndexChanged.emit(self._letter_combo.currentIndex())
        QTimer.singleShot(0, lambda: self._rebuild_keyword_combo() if self._cascade_id == _cid else None)

    def _rebuild_keyword_combo(self) -> None:
        """Rebuild the Keyword combo for the current item+field+letter scope."""
        self._cascade_id += 1
        _cid = self._cascade_id
        scope  = self._item_combo.currentData()
        field  = self._field_combo.currentData()  or "--all"
        letter = self._letter_combo.currentData() or "--all"

        if not scope and field == "--all" and self._filter_cache:
            # All filters at top level: derive counts from the pre-built cache.
            all_counts = self._filter_cache["counts"]
            if letter == "--all":
                counts = all_counts
            elif letter == "#":
                counts = {lbl: n for lbl, n in all_counts.items()
                          if lbl and not lbl[0].isalpha()}
            else:
                counts = {lbl: n for lbl, n in all_counts.items()
                          if lbl[:1].upper() == letter}
        else:
            records = self._all_items
            if scope:
                records = [
                    r for r in records
                    if _clean_stem(r.get("filename_stem", "")) == scope
                ]
            if field != "--all":
                records = [
                    r for r in records
                    if (r.get("field") or "--all") == field
                ]
            if letter != "--all":
                if letter == "#":
                    records = [
                        r for r in records
                        if r.get("label", "") and not r["label"][0].isalpha()
                    ]
                else:
                    records = [
                        r for r in records
                        if r.get("label", "")[:1].upper() == letter
                    ]
            counts: dict[str, int] = {}
            for r in records:
                lbl = r.get("label", "")
                if lbl:
                    counts[lbl] = counts.get(lbl, 0) + 1
        labels = sorted(counts.keys(), key=str.casefold)

        prev = self._keyword_combo.currentData()
        self._keyword_combo.blockSignals(True)
        self._keyword_combo.clear()
        self._keyword_combo.addItem("<Keyword>", userData="--all")
        for lbl in labels:
            self._keyword_combo.addItem(f"{lbl}  ({counts[lbl]})", userData=lbl)
        idx = self._keyword_combo.findData(prev)
        self._keyword_combo.setCurrentIndex(max(0, idx))
        self._keyword_combo.blockSignals(False)
        self._keyword_combo.currentIndexChanged.emit(self._keyword_combo.currentIndex())
        QTimer.singleShot(0, lambda: self._apply_filters() if self._cascade_id == _cid else None)

    def _apply_filters(self) -> None:
        """Apply all five filter levels and rebuild the grid."""
        _cid = self._cascade_id   # capture — don't increment here
        scope   = self._item_combo.currentData()
        field   = self._field_combo.currentData()  or "--all"
        letter  = self._letter_combo.currentData() or "--all"
        keyword = self._keyword_combo.currentData() or "--all"

        records = self._all_items
        if scope:
            records = [
                r for r in records
                if _clean_stem(r.get("filename_stem", "")) == scope
            ]
        if field != "--all":
            records = [
                r for r in records
                if (r.get("field") or "--all") == field
            ]
        if letter != "--all":
            if letter == "#":
                records = [
                    r for r in records
                    if r.get("label", "") and not r["label"][0].isalpha()
                ]
            else:
                records = [
                    r for r in records
                    if r.get("label", "")[:1].upper() == letter
                ]
        if keyword != "--all":
            records = [r for r in records if r.get("label", "") == keyword]

        self._filtered_items = records
        # Emit the active keyword so the Filter section title stays current.
        kw_data = self._keyword_combo.currentData()
        self.keywordChanged.emit(kw_data if (kw_data and kw_data != "--all") else "")
        # Defer the grid rebuild one final tick so the UI reflects the updated
        # combo state and status bar before the (potentially slow) cell creation.
        QTimer.singleShot(0, lambda: self._rebuild_grid() if self._cascade_id == _cid else None)

    # ------------------------------------------------------------------ grid

    def _cols(self) -> int:
        """Compute number of grid columns from the current viewport width."""
        vw = self._scroll.viewport().width()
        if vw <= 0:
            vw = 200
        return max(1, (vw - _THUMB_GAP) // (self._thumb_size + _THUMB_GAP))

    @property
    def _page_size(self) -> int:
        """Dynamic page size: exactly the thumbnails that fit in the viewport.

        Columns × rows, where rows is derived from the scroll-area viewport
        height the same way columns are derived from its width.  Falls back
        to a 3-row default when the viewport height is not yet known.
        """
        cols = self._cols()
        vh   = self._scroll.viewport().height()
        if vh <= 0:
            return max(1, cols * 3)
        rows = max(1, (vh - _THUMB_GAP) // (self._thumb_size + _THUMB_GAP))
        return max(1, cols * rows)

    def _current_page_records(self) -> list[dict]:
        start = self._page_index * self._page_size
        return self._filtered_items[start: start + self._page_size]

    def _rebuild_grid(self) -> None:
        """Clear the grid and populate it with ThumbnailCell widgets for the
        current page of filtered items."""
        self._stop_loader()

        # Remove all existing cells from the layout and destroy them.
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._cells = []

        page_records = self._current_page_records()
        cols      = self._cols()
        self._grid_cols = cols   # remember for navigate_grid
        abs_start = self._page_index * self._page_size

        for i, rec in enumerate(page_records):
            label       = rec.get("label", "")
            stem        = rec.get("filename_stem", "")
            frame       = rec.get("frame", "")
            is_best     = bool(rec.get("human_best"))
            abs_idx     = abs_start + i
            is_selected = abs_idx == self._selected_index

            tip = f"{label}  {_clean_stem(stem)}"
            if frame:
                tip += f"  f:{frame}"

            cell = ThumbnailCell(index=i, size=self._thumb_size, tooltip=tip)
            cell.set_light_bg(self._light_bg)
            cell.set_selected(is_selected)
            cell.set_highlighted(is_best)
            cell.clicked.connect(self._on_cell_clicked)
            cell.doubleClicked.connect(self._on_cell_double_clicked)

            # Build drag-and-drop payload via source thumbnail resolution so
            # non-JSON-backed sources (e.g. engraving output paths) can drag
            # their actual image file instead of a derived sidecar path.
            drag_path = self._source.thumbnail_path(rec)
            if drag_path is None:
                path_raw = rec.get("path")
                if path_raw:
                    drag_path = Path(str(path_raw)).with_suffix(".png")
            if drag_path is not None:
                cell.drag_path = str(drag_path)
                cell.drag_meta = {
                    k: str(v) if isinstance(v, Path) else v
                    for k, v in rec.items()
                    if not isinstance(v, (dict, list))
                }

            self._grid_layout.addWidget(cell, i // cols, i % cols)
            self._cells.append(cell)

        self._update_status()
        self._update_pagination()
        self._start_loader()

    def _start_loader(self) -> None:
        """Start a ThumbnailLoader for the current page records."""
        page_records = self._current_page_records()
        if not page_records:
            return
        self._loader = ThumbnailLoader(
            page_records, self._thumb_size, path_for=self._source.thumbnail_path
        )
        self._loader.thumbReady.connect(self._on_thumb_ready)
        self._loader.start()

    def _stop_loader(self) -> None:
        """Cancel any running loader and wait for it to stop."""
        if self._loader:
            # Disconnect before cancelling so stale thumbReady signals
            # queued in the event loop do not update the (already rebuilt) cells.
            try:
                self._loader.thumbReady.disconnect(self._on_thumb_ready)
            except (TypeError, RuntimeError):
                pass
            if self._loader.isRunning():
                self._loader.cancel()
                self._loader.wait(300)
        self._loader = None

    def _on_thumb_ready(self, idx: int, qimg: QImage) -> None:
        if 0 <= idx < len(self._cells):
            self._cells[idx].set_image(qimg)

    def _update_status(self) -> None:
        pass  # 'No items' is now shown in the pagination bar via _update_pagination

    def _update_pagination(self) -> None:
        total      = len(self._filtered_items)
        page_count = max(1, math.ceil(total / self._page_size))
        if total == 0:
            self._page_lbl.setText("No items")
            self._prev_btn.setEnabled(False)
            self._next_btn.setEnabled(False)
        else:
            self._page_lbl.setText(
                f"{self._page_index + 1} / {page_count} ({total})"
            )
            self._prev_btn.setEnabled(self._page_index > 0)
            self._next_btn.setEnabled(self._page_index < page_count - 1)

    # ------------------------------------------------------------------ selection

    def _on_cell_clicked(self, idx: int) -> None:
        abs_idx = self._page_index * self._page_size + idx

        # Deselect the previously selected cell (if it is on this page)
        prev_page_idx = self._selected_index - self._page_index * self._page_size
        if 0 <= prev_page_idx < len(self._cells):
            self._cells[prev_page_idx].set_selected(False)

        self._selected_index = abs_idx

        if 0 <= idx < len(self._cells):
            self._cells[idx].set_selected(True)

        item = self.currentItem()
        if item is not None:
            self.selectionChanged.emit(item)

    def _on_cell_double_clicked(self, idx: int) -> None:
        self._on_cell_clicked(idx)   # keep selection state consistent
        item = self.currentItem()
        if item is not None:
            self.itemActivated.emit(item)

    # ------------------------------------------------------------------ pagination

    def _on_prev_page(self) -> None:
        if self._page_index > 0:
            self._page_index -= 1
            self._selected_index = self._page_index * self._page_size  # jump to first cell
            self._rebuild_grid()

    def _on_next_page(self) -> None:
        page_count = max(
            1, math.ceil(len(self._filtered_items) / self._page_size)
        )
        if self._page_index < page_count - 1:
            self._page_index += 1
            self._selected_index = self._page_index * self._page_size  # jump to first cell
            self._rebuild_grid()

    # ------------------------------------------------------------------ keyboard navigation

    @staticmethod
    def _step_combo(combo: QComboBox, delta: int) -> None:
        """Advance *combo* by *delta* steps, wrapping at the ends."""
        n = combo.count()
        if n == 0:
            return
        combo.setCurrentIndex((combo.currentIndex() + delta) % n)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        mod = event.modifiers()
        shift = bool(mod & Qt.ShiftModifier) and not (
            mod & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)
        )
        if key == Qt.Key_Home:
            self._step_combo(self._item_combo, -1)
        elif key == Qt.Key_End:
            self._step_combo(self._item_combo, 1)
        elif key == Qt.Key_PageUp:
            self._step_combo(self._field_combo, -1)
        elif key == Qt.Key_PageDown:
            self._step_combo(self._field_combo, 1)
        elif key == Qt.Key_Up:
            if shift:
                self._step_combo(self._keyword_combo, -1)
            else:
                self.navigate_grid(0, -1)
        elif key == Qt.Key_Down:
            if shift:
                self._step_combo(self._keyword_combo, 1)
            else:
                self.navigate_grid(0, 1)
        elif key == Qt.Key_Left:
            if shift:
                self._on_prev_page()
            else:
                self.navigate_grid(-1, 0)
        elif key == Qt.Key_Right:
            if shift:
                self._on_next_page()
            else:
                self.navigate_grid(1, 0)
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Redirect navigation keys from combo dropdowns to the browser."""
        if event.type() == QEvent.KeyPress:
            nav_keys = {
                Qt.Key_Home, Qt.Key_End,
                Qt.Key_PageUp, Qt.Key_PageDown,
                Qt.Key_Up, Qt.Key_Down,
                Qt.Key_Left, Qt.Key_Right,
            }
            if event.key() in nav_keys:
                self.keyPressEvent(event)
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ resize

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Re-layout the grid when the browser is resized."""
        super().resizeEvent(event)
        if self._cells:
            cols = self._cols()
            self._grid_cols = cols   # keep in sync with the new layout
            for i, cell in enumerate(self._cells):
                self._grid_layout.addWidget(cell, i // cols, i % cols)
        # Rebuild after the user stops dragging the window edge.
        self._resize_timer.start(120)

    def _on_resize_settled(self) -> None:
        """Called ~120 ms after the last resize event.

        Rebuilds the grid if the viewport now fits a different number of
        thumbnails than are currently shown.
        """
        if not self._filtered_items:
            return
        new_size = self._page_size
        if new_size != len(self._cells):
            self._page_index = 0
            self._rebuild_grid()

