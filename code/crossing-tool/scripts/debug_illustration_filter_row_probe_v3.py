#!/usr/bin/env python3
"""Illustration Filter-Row Width Leak Probe — DEBUG_LAYOUT_PROBE_V3.

Diagnostic tool only — makes NO permanent changes to application behavior
or persisted preferences (all `tool.prefs` reads/writes are neutralized
in-process for the duration of the temp-project measurements below; the
one exception is the banner section, which deliberately reads the REAL,
currently active project's prefs with the real unpatched functions, purely
to prove we are looking at live current state and not a stale/cached read).

WHY THIS SCRIPT EXISTS (anti-staleness):
We have changed this framework/visualizer many times across this session.
Every run of this probe prints a version-stamped banner with the exact
module file paths, function source hashes, git commit, and working-tree
dirty state so it is never ambiguous whether a given run's output reflects
the code currently on disk, versus stale bytecode/an old widget instance/
a leftover persisted-layout artifact from a previous bug.

CURRENT HYPOTHESIS UNDER TEST (not assumed true):
The loaded-state width leak is suspected to come from the Illustration
Filter row's content (movie/title selector combo, pagination row, or the
Filter section itself) once the catalog is populated — NOT from the shared
framework, and the page counter is explicitly NOT assumed guilty (treat it
as a symptom, not a cause, until this probe proves otherwise).

STAGES:
    1. empty Illustration startup
    2. after catalog load, before selection
    3. after first selection
    4. after manually widening the inspector
    5. switch Silhouettes -> Engravings and repeat the catalog-loaded check

Then the same kind of load-state check is run against Metadata for
comparison.

Usage:
    QT_QPA_PLATFORM=offscreen python3 scripts/debug_illustration_filter_row_probe_v3.py
"""
from __future__ import annotations

PROBE_NAME = "Illustration Filter-Row Width Leak Probe"
PROBE_VERSION = "DEBUG_LAYOUT_PROBE_V12"
# Bump this tag (V8, V9, ...) every time this script is revised so old
# terminal output can never be mistaken for a new run. V4 fixed a probe-only
# bug (not an app bug): the injected fake catalog items were not reaching
# the item/field/letter combos because `_filter_cache` (populated once by
# the real background _CatalogLoader against the empty temp project) was
# never invalidated before `refresh()`. V5 fixes a probe-only crash: a
# still-running background QThread (catalog/thumbnail loader) was being
# destroyed mid-flight, which PyQt5 treats as fatal and aborts the process.
# V6 fixes the same class of probe-only crash for the Metadata window's own
# thumbnail-loader thread. V7 adds stages 5a/5b/5c to disambiguate WHEN an
# Inspector.sizeHint() jump on tab switch actually occurs (hidden-tab load
# vs. mount-time vs. reversibility on switching back).

import hashlib
import inspect
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from PyQt5.QtWidgets import QApplication, QComboBox, QLabel, QWidget  # noqa: E402

from visualizers.illustration_visualizer import IllustrationWindow, IllustrationPane  # noqa: E402
from visualizers.components.illustration_browser import IllustrationBrowser  # noqa: E402
from visualizers.metadata_visualizer import MetadataVisualizer  # noqa: E402
from visualizers.components.collapsible_section import CollapsibleSection  # noqa: E402


# ---------------------------------------------------------------------------
# Anti-staleness banner
# ---------------------------------------------------------------------------

def _git_commit_and_dirty() -> tuple[str, str]:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception as exc:
        commit = f"(git rev-parse failed: {exc})"
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=_REPO_ROOT, text=True, stderr=subprocess.DEVNULL,
        )
        dirty = "DIRTY (uncommitted changes present)" if status.strip() else "clean"
    except Exception as exc:
        dirty = f"(git status failed: {exc})"
    return commit, dirty


def _src_hash(fn) -> str:
    """Short sha256 of the ACTUAL currently-loaded source of *fn* — proves
    the bytes being executed right now, not a description of what we
    believe the code does."""
    try:
        src = inspect.getsource(fn)
        return hashlib.sha256(src.encode()).hexdigest()[:12]
    except Exception as exc:
        return f"(unavailable: {exc})"


def print_banner() -> None:
    print("#" * 100)
    print(f"# {PROBE_NAME}")
    print(f"# {PROBE_VERSION}")
    print("#" * 100)

    commit, dirty = _git_commit_and_dirty()
    print(f"probe script path         : {Path(__file__).resolve()}")
    print(f"git commit (short)        : {commit}")
    print(f"working tree state        : {dirty}")
    print()
    print("Anti-staleness — exact code being exercised:")
    print(f"  IllustrationWindow.__module__ : {IllustrationWindow.__module__}")
    print(f"  IllustrationWindow file       : {inspect.getfile(IllustrationWindow)}")
    print(f"  IllustrationPane.__module__   : {IllustrationPane.__module__}")
    print(f"  IllustrationPane file         : {inspect.getfile(IllustrationPane)}")
    print(f"  IllustrationBrowser.__module__: {IllustrationBrowser.__module__}")
    print(f"  IllustrationBrowser file      : {inspect.getfile(IllustrationBrowser)}")
    print(f"  MetadataVisualizer.__module__ : {MetadataVisualizer.__module__}")
    print(f"  MetadataVisualizer file       : {inspect.getfile(MetadataVisualizer)}")
    print()
    print("  Function source hashes (sha256[:12] of inspect.getsource(...) —")
    print("  compare across runs to instantly detect a stale/cached import):")
    print(f"    IllustrationWindow._restore_saved_state       {_src_hash(IllustrationWindow._restore_saved_state)}")
    print(f"    IllustrationPane._build_source_panel          {_src_hash(IllustrationPane._build_source_panel)}")
    print(f"    IllustrationBrowser._build_filter_section     {_src_hash(IllustrationBrowser._build_filter_section)}")
    print(f"    IllustrationBrowser._build_pagination_bar     {_src_hash(IllustrationBrowser._build_pagination_bar)}")
    print(f"    IllustrationBrowser._update_pagination        {_src_hash(IllustrationBrowser._update_pagination)}")
    print(f"    IllustrationBrowser._rebuild_item_combo       {_src_hash(IllustrationBrowser._rebuild_item_combo)}")

    print()
    print("Real, CURRENTLY ACTIVE project prefs (read with the real, unpatched")
    print("tool.prefs.get — proves we are reading live current state, not a")
    print("stale snapshot). NOTE: this run's own measurements below use a")
    print("separate, sandboxed temp project with prefs fully neutralized —")
    print("nothing below is read from or written to this real project.")
    from tool import prefs as _prefs
    print(f"  active project path (~/.crossing/prefs.json 'path') = {_prefs.get('path')!r}")
    illustration_keys = [
        "window_illustration_panel_sizes",
        "window_illustration_browser_mode",
        "window_illustration_fullscreen",
        "window_illustration",
        "ill_media_type",
        "ill_sil_section_filter", "ill_sil_section_sort",
        "ill_sil_section_info", "ill_sil_section_tools",
        "ill_eng_section_filter", "ill_eng_section_info",
        "ill_eng_section_mode", "ill_eng_section_tools",
        "ill_section_tools",
    ]
    for k in illustration_keys:
        print(f"  {k:35s} = {_prefs.get(k)!r}")
    print("#" * 100)


# ---------------------------------------------------------------------------
# Widget measurement helpers
# ---------------------------------------------------------------------------

_POLICY_NAMES = {
    0: "Fixed", 1: "Minimum", 4: "Maximum", 5: "Preferred",
    3: "MinimumExpanding", 7: "Expanding", 13: "Ignored",
}


def _policy_str(w: QWidget) -> str:
    sp = w.sizePolicy()
    h = _POLICY_NAMES.get(int(sp.horizontalPolicy()), sp.horizontalPolicy())
    v = _POLICY_NAMES.get(int(sp.verticalPolicy()), sp.verticalPolicy())
    return f"h={h} v={v}"


def _fmt(w: QWidget, label: str) -> str:
    sh, msh = w.sizeHint(), w.minimumSizeHint()
    extra = ""
    if isinstance(w, QComboBox):
        extra = f" count={w.count()} currentText={w.currentText()!r}"
    elif isinstance(w, QLabel):
        extra = f" text={w.text()!r}"
    return (
        f"  {label:40s} [{type(w).__name__:16s}] "
        f"sizeHint={sh.width():4d}x{sh.height():<4d} "
        f"minimumSizeHint={msh.width():4d}x{msh.height():<4d} "
        f"minimumWidth={w.minimumWidth():4d} maximumWidth={w.maximumWidth():6d} "
        f"sizePolicy=({_policy_str(w)}){extra}"
    )


def _viewport_width(panel) -> "int | str":
    try:
        return panel._content_scroll.viewport().width()
    except Exception as exc:  # pragma: no cover - diagnostic fallback only
        return f"(unavailable: {exc})"


# ---------------------------------------------------------------------------
# Illustration stage measurement
# ---------------------------------------------------------------------------

def measure_illustration_stage(win: IllustrationWindow, stage_name: str, source: str) -> dict:
    print("\n" + "=" * 100)
    print(f"ILLUSTRATION STAGE: {stage_name}  (active source = {source})")
    print("=" * 100)

    cat = win._catalog
    inspector = win._inspector_shell
    tabbed = inspector.tabbed_panel()
    idx = tabbed.currentIndex()
    tab_title = tabbed.tab_bar().tabText(idx).strip()
    panel = tabbed.currentWidget()
    browser = cat._browser_sil if source == "sil" else cat._browser_eng

    viewport_w = _viewport_width(panel)

    print(f"splitter.sizes()                     = {list(win._splitter.sizes())}")
    print(f"Inspector.sizeHint()                 = {inspector.sizeHint().width()}x{inspector.sizeHint().height()}")
    print(f"Inspector.minimumSizeHint()           = {inspector.minimumSizeHint().width()}x{inspector.minimumSizeHint().height()}")
    print(f"active tab                           = index {idx} ({tab_title!r})")
    print(f"active tab TabPanel.sizeHint()        = {panel.sizeHint().width()}x{panel.sizeHint().height()}")
    print(f"TabPanel content viewport width       = {viewport_w}")

    filter_sections = [s for s in panel.findChildren(CollapsibleSection) if s._title == "Filter"]
    fs = filter_sections[0] if filter_sections else None
    if fs is not None:
        print(f"Filter CollapsibleSection.sizeHint() = {fs.sizeHint().width()}x{fs.sizeHint().height()}  expanded={fs.is_expanded()}")
    else:
        print("Filter CollapsibleSection             = NOT FOUND")

    widgets = [
        ("page counter (_page_lbl)", browser._page_lbl),
        ("prev button (_prev_btn)", browser._prev_btn),
        ("next button (_next_btn)", browser._next_btn),
        ("media selector (_media_combo)", browser._media_combo),
        ("movie/title selector (_item_combo)", browser._item_combo),
        ("field selector (_field_combo)", browser._field_combo),
        ("letter selector (_letter_combo)", browser._letter_combo),
        ("keyword selector (_keyword_combo)", browser._keyword_combo),
        ("pagination row (parent of page_lbl)", browser.pagination_panel),
        ("filter column (parent of 5 combos)", browser.filter_panel),
    ]
    if fs is not None:
        widgets.append(("Filter CollapsibleSection (parent row)", fs))

    print("\n-- Filter-row / pagination widgets --")
    for label, w in widgets:
        print(_fmt(w, label))

    print("\n-- vs. available TabPanel content width --")
    if isinstance(viewport_w, int):
        for label, w in widgets:
            msh_w = w.minimumSizeHint().width()
            verdict = "OVERFLOWS" if msh_w > viewport_w else "fits"
            print(f"  {label:40s} minimumSizeHint.w={msh_w:4d}  viewport={viewport_w:4d}  -> {verdict}")
    else:
        print(f"  (viewport width unavailable: {viewport_w})")

    return {
        "stage": stage_name,
        "source": source,
        "splitter_sizes": list(win._splitter.sizes()),
        "inspector_sizehint_w": inspector.sizeHint().width(),
        "viewport_w": viewport_w,
        "item_combo_sizehint_w": browser._item_combo.sizeHint().width(),
        "item_combo_minsizehint_w": browser._item_combo.minimumSizeHint().width(),
        "page_lbl_sizehint_w": browser._page_lbl.sizeHint().width(),
        "page_lbl_text": browser._page_lbl.text(),
        "filter_section_sizehint_w": fs.sizeHint().width() if fs is not None else None,
    }


# ---------------------------------------------------------------------------
# Metadata comparison stage
# ---------------------------------------------------------------------------

def measure_metadata_stage(win: MetadataVisualizer, stage_name: str) -> dict:
    print("\n" + "=" * 100)
    print(f"METADATA STAGE: {stage_name}")
    print("=" * 100)

    inspector = win._inspector
    tabbed = inspector.tabbed_panel()
    idx = tabbed.currentIndex()
    tab_title = tabbed.tab_bar().tabText(idx).strip()
    panel = tabbed.currentWidget()
    viewport_w = _viewport_width(panel)

    print(f"splitter.sizes()                     = {list(win._splitter.sizes())}")
    print(f"Inspector.sizeHint()                 = {inspector.sizeHint().width()}x{inspector.sizeHint().height()}")
    print(f"active tab                           = index {idx} ({tab_title!r})")
    print(f"active tab TabPanel.sizeHint()        = {panel.sizeHint().width()}x{panel.sizeHint().height()}")
    print(f"TabPanel content viewport width       = {viewport_w}")

    print(
        "\nMetadata's Inspector has NO Filter section, NO combo cascade, and "
        "NO pagination widget at all (confirmed by direct inspection of "
        "metadata_visualizer.py — _build_source_tab_panel only ever builds "
        "Thumbnail/Info/Tools sections). There is no structurally equivalent "
        "widget to the Illustration Filter row to compare against. Nearest "
        "comparable case: the Info section's own content-driven value "
        "QLabels (e.g. 'filename'), printed below for reference only."
    )
    media_type = "movie"
    info_block = win._info_blocks[media_type]
    for key, lbl in info_block.labels().items():
        print(_fmt(lbl, f"Info[{key}]"))

    return {
        "stage": stage_name,
        "splitter_sizes": list(win._splitter.sizes()),
        "inspector_sizehint_w": inspector.sizeHint().width(),
        "viewport_w": viewport_w,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print_banner()

    app = QApplication.instance() or QApplication(sys.argv)

    # Fully sandbox prefs for every measurement below: never read the real
    # project's persisted values (already proven live/current in the banner
    # above with the real function) and never write anything, for either
    # visualizer under test.
    from tool import prefs as _prefs
    _real_get, _real_set = _prefs.get, _prefs.set
    _prefs.get = lambda k, d=None: d
    _prefs.set = lambda k, v: None

    illustration_stages: list[dict] = []
    metadata_stages: list[dict] = []

    # ------------------------------------------------------------ Illustration
    ill_project_dir = tempfile.mkdtemp(prefix="crossing_v3_probe_ill_")
    print(f"\n[Illustration] sandboxed temp project dir = {ill_project_dir}")

    win = IllustrationWindow(ill_project_dir)
    win.show()
    app.processEvents()
    app.processEvents()
    illustration_stages.append(measure_illustration_stage(win, "1) STARTUP (empty catalog, no selection)", "sil"))

    cat = win._catalog
    fake_sil_items = [
        {
            "label": "man-standing-in-doorway-holding-rifle",
            "filename_stem": "IlBuonoIlBruttoIlCattivo 1966 The Good The Bad And The Ugly",
            "filename": "IlBuonoIlBruttoIlCattivo 1966 The Good The Bad And The Ugly.mkv",
            "shot_id": "sh_00042_0000123456789", "frame": 12345,
            "sam_model": "sam3.pt", "confidence_score": 0.912, "human_best": False,
            "mask_area": 5000, "frame_size": [1920, 800], "bbox": [10, 10, 200, 300],
            "path": str(Path(ill_project_dir) / "fake_object_1.json"),
        },
        {
            "label": "woman-seated-at-table-drinking-coffee",
            "filename_stem": "a_very_long_underscore_joined_rip_filename_with_no_spaces_or_hyphens_at_all_1967",
            "filename": "a_very_long_underscore_joined_rip_filename_with_no_spaces_or_hyphens_at_all_1967.mkv",
            "shot_id": "sh_00099_0000987654321", "frame": 999,
            "sam_model": "sam3.pt", "confidence_score": 0.5, "human_best": True,
            "mask_area": 1200, "frame_size": [1920, 800], "bbox": [1, 2, 3, 4],
            "path": str(Path(ill_project_dir) / "fake_object_2.json"),
        },
    ] * 5000  # ~10,000 items — realistic large-corpus scale for a full page-counter/combo test
    cat._sil_source.items = lambda: list(fake_sil_items)
    # `refresh()` does NOT invalidate `_filter_cache` (only `reload()` does) —
    # the real background `_CatalogLoader` already populated it once, from
    # the (empty) temp project, during the deferred startup `reload()` above.
    # Without clearing it here the item/field/letter combos would keep using
    # that stale empty cache and never see the injected fake items. This is
    # read/write on the *browser instance's own state* for this diagnostic
    # run only — it does not change any browser/source code on disk.
    cat._browser_sil._filter_cache = {}
    cat._browser_sil.refresh()
    app.processEvents()
    app.processEvents()
    illustration_stages.append(measure_illustration_stage(win, "2) CATALOG LOADED, BEFORE SELECTION", "sil"))

    cat._on_selection_changed(fake_sil_items[1])  # select the long, break-point-free-name item
    app.processEvents()
    app.processEvents()
    illustration_stages.append(measure_illustration_stage(win, "3) OBJECT SELECTED", "sil"))

    splitter = win._splitter
    total = sum(splitter.sizes())
    wider = min(total - 100, max(splitter.sizes()[1] + 400, 700))
    splitter.setSizes([max(100, total - wider), wider])
    app.processEvents()
    app.processEvents()
    illustration_stages.append(measure_illustration_stage(win, "4) INSPECTOR MANUALLY WIDENED", "sil"))

    fake_eng_items = [dict(item, path=str(Path(ill_project_dir) / "fake_eng.json")) for item in fake_sil_items[:2]] * 5000
    cat._eng_source.items = lambda: list(fake_eng_items)
    cat._browser_eng._filter_cache = {}  # see comment above re: stale empty cache
    cat._browser_eng.refresh()
    app.processEvents()
    app.processEvents()
    # Measure BEFORE switching tabs — TabbedPanel's docstring claims hidden
    # pages are fully unparented and can never influence sizeHint(). Test
    # that claim directly instead of trusting the comment.
    print("\n" + "!" * 100)
    print("!! CHECKPOINT 5a: Engravings catalog populated (grown Filter/item combo)")
    print("!! but SILHOUETTES IS STILL THE ACTIVE/MOUNTED TAB — did the hidden")
    print("!! Engravings page's growth leak into Inspector.sizeHint() anyway?")
    print("!" * 100)
    illustration_stages.append(measure_illustration_stage(win, "5a) ENG CATALOG LOADED, SIL STILL ACTIVE (hidden-tab check)", "sil"))

    win._inspector_shell.tabbed_panel().setCurrentIndex(1)
    app.processEvents()
    app.processEvents()
    illustration_stages.append(measure_illustration_stage(win, "5b) SWITCHED TO ENGRAVINGS, CATALOG LOADED", "eng"))

    win._inspector_shell.tabbed_panel().setCurrentIndex(0)
    app.processEvents()
    app.processEvents()
    illustration_stages.append(measure_illustration_stage(win, "5c) SWITCHED BACK TO SILHOUETTES (reversibility check)", "sil"))

    # Stop background QThreads (catalog loader / thumbnail loader) explicitly
    # before tearing down the window — otherwise a still-running QThread can
    # be destroyed mid-flight when this script moves on, which PyQt5 treats
    # as fatal ("QThread: Destroyed while thread is still running") and
    # aborts the whole process. This is a probe-script cleanup concern only,
    # not an application bug.
    for _browser in (cat._browser_sil, cat._browser_eng):
        _browser._stop_catalog_loader()
        _browser._stop_loader()
    win._ipc_server.stop()
    win._ipc_server.wait(500)
    win.close()
    app.processEvents()
    shutil.rmtree(ill_project_dir, ignore_errors=True)

    # ------------------------------------------------------------ Metadata
    md_project_dir = tempfile.mkdtemp(prefix="crossing_v3_probe_md_")
    print(f"\n[Metadata] sandboxed temp project dir = {md_project_dir}")

    mwin = MetadataVisualizer(md_project_dir)
    mwin.show()
    app.processEvents()
    app.processEvents()
    metadata_stages.append(measure_metadata_stage(mwin, "1) STARTUP (empty catalog, no selection)"))

    fake_movie_record = {
        "title": "A Very Long Movie Title That Keeps Going And Going (1967)",
        "filename": "a_very_long_underscore_joined_rip_filename_with_no_spaces_or_hyphens_at_all_1967.mkv",
        "original_filename": "a_very_long_underscore_joined_rip_filename_with_no_spaces_or_hyphens_at_all_1967.mkv",
        "media_type": "movie", "year": "1967", "director": "Someone Somebody",
        "duration": "161 min", "tagline": "A tagline.", "overview": "An overview.",
        "media_id": "tmdb_12345", "tmdb": "12345", "imdb": "tt0001234",
    }
    mwin._movie_page.set_records([fake_movie_record])
    mwin._on_page_selection_changed("movie", fake_movie_record)
    app.processEvents()
    app.processEvents()
    metadata_stages.append(measure_metadata_stage(mwin, "2) RECORD LOADED + SELECTED (long filename)"))

    mwin._movie_page._thumb_manager.stop()
    mwin.close()
    app.processEvents()
    shutil.rmtree(md_project_dir, ignore_errors=True)

    _prefs.get, _prefs.set = _real_get, _real_set

    # ------------------------------------------------------------ Verdict
    print("\n\n" + "#" * 100)
    print("VERDICT (derived from the numbers printed above, not assumed)")
    print("#" * 100)

    s1, s2 = illustration_stages[0], illustration_stages[1]
    print(f"\nIllustration page counter (_page_lbl) sizeHint width: "
          f"{s1['page_lbl_sizehint_w']} ({s1['page_lbl_text']!r}) -> "
          f"{s2['page_lbl_sizehint_w']} ({s2['page_lbl_text']!r})")
    print(f"Illustration movie/title combo (_item_combo) sizeHint width: "
          f"{s1['item_combo_sizehint_w']} -> {s2['item_combo_sizehint_w']} "
          f"(minimumSizeHint width -> {s2['item_combo_minsizehint_w']})")
    print(f"Inspector.sizeHint().width() across all 5 stages: "
          f"{[st['inspector_sizehint_w'] for st in illustration_stages]}")
    print(f"splitter.sizes() across all 5 stages: "
          f"{[st['splitter_sizes'] for st in illustration_stages]}")

    inspector_w_changed = len({st['inspector_sizehint_w'] for st in illustration_stages}) > 1
    if inspector_w_changed:
        print("\n>>> Inspector.sizeHint() CHANGED across stages — some widget's "
              "growth IS propagating upward. Re-examine the per-stage tables "
              "above for the 'OVERFLOWS' markers to find which one.")
    else:
        print("\n>>> Inspector.sizeHint() did NOT change across any stage, "
              "despite the movie/title combo's own sizeHint growing "
              "(see above). This confirms (again, numerically, this run) "
              "that Filter-row content is firewalled from the Inspector's "
              "own size by TabPanel's internal QScrollArea and cannot be "
              "the source of a startup/loaded-state width leak.")

    print(
        "\nMetadata has no equivalent Filter/pagination widget at all — "
        "its Inspector.sizeHint() across its 2 stages: "
        f"{[st['inspector_sizehint_w'] for st in metadata_stages]}"
    )


if __name__ == "__main__":
    main()
