#!/usr/bin/env python3
"""Read-only layout instrumentation for the Illustration inspector.

Diagnostic tool only — makes NO changes to application behavior. Walks the
Window -> Inspector -> TabbedPanel -> current Tab -> TabPanel ->
CollapsibleSection -> MetadataBlock/button-row -> QLabel/QPushButton/layout
tree and prints, for every object:

    class, object name, sizeHint(), minimumSizeHint(), minimumWidth(),
    maximumWidth(), current width(), sizePolicy(), geometry()

at four lifecycle stages:

    1. startup            (window shown, initial event-loop pass)
    2. catalog loaded      (browser source populated + refreshed)
    3. object selected     (first item selection simulated)
    4. inspector widened   (splitter given more width, as if user-dragged)

Then diffs stage-to-stage (by stable tree path, since object identity is
consistent run-to-run) and reports the FIRST widget (in top-down traversal
order) whose minimumSizeHint().width() or sizeHint().width() increased.

Usage:
    QT_QPA_PLATFORM=offscreen python3 scripts/debug_illustration_inspector_layout.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLayout, QLabel, QPushButton, QScrollArea,
)

from visualizers.illustration_visualizer import IllustrationWindow
from visualizers.components.tabbed_panel import TabbedPanel
from visualizers.components.tab_panel import TabPanel
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.metadata_block import MetadataBlock
from visualizers.components.inspector import Inspector


def _policy_str(w: QWidget) -> str:
    sp = w.sizePolicy()
    hpol = sp.horizontalPolicy()
    vpol = sp.verticalPolicy()
    names = {
        0: "Fixed", 1: "Minimum", 4: "Maximum", 5: "Preferred",
        3: "MinimumExpanding", 7: "Expanding", 13: "Ignored",
    }
    return (f"h={names.get(int(hpol), hpol)}(stretch={sp.horizontalStretch()})"
            f" v={names.get(int(vpol), vpol)}(stretch={sp.verticalStretch()})")


def _rect_str(r) -> str:
    return f"({r.x()},{r.y()},{r.width()}x{r.height()})"


def _size_str(s) -> str:
    return f"{s.width()}x{s.height()}"


class _Node:
    __slots__ = ("path", "cls", "name", "size_hint_w", "min_size_hint_w",
                 "min_w", "max_w", "width", "line")

    def __init__(self, path, cls, name, size_hint_w, min_size_hint_w,
                 min_w, max_w, width, line):
        self.path = path
        self.cls = cls
        self.name = name
        self.size_hint_w = size_hint_w
        self.min_size_hint_w = min_size_hint_w
        self.min_w = min_w
        self.max_w = max_w
        self.width = width
        self.line = line


def describe_widget(w: QWidget, path: str, depth: int, out_lines: list, nodes: dict) -> None:
    indent = "  " * depth
    cls = type(w).__name__
    name = w.objectName() or "(none)"
    sh = w.sizeHint()
    msh = w.minimumSizeHint()
    geo = w.geometry()
    line = (
        f"{indent}[{cls}] name={name!r} sizeHint={_size_str(sh)} "
        f"minimumSizeHint={_size_str(msh)} minimumWidth={w.minimumWidth()} "
        f"maximumWidth={w.maximumWidth()} width={w.width()} "
        f"sizePolicy=({_policy_str(w)}) geometry={_rect_str(geo)}"
    )
    out_lines.append(line)
    nodes[path] = _Node(path, cls, name, sh.width(), msh.width(),
                         w.minimumWidth(), w.maximumWidth(), w.width(), line)

    lay = w.layout()
    if lay is not None:
        describe_layout(lay, path + "/layout", depth + 1, out_lines, nodes)


def describe_layout(lay: QLayout, path: str, depth: int, out_lines: list, nodes: dict) -> None:
    indent = "  " * depth
    cls = type(lay).__name__
    sh = lay.sizeHint()
    geo = lay.geometry()
    line = (
        f"{indent}[{cls} layout] sizeHint={_size_str(sh)} "
        f"minimumSize={_size_str(lay.minimumSize())} geometry={_rect_str(geo)}"
    )
    out_lines.append(line)
    nodes[path] = _Node(path, cls, "(layout)", sh.width(),
                         lay.minimumSize().width(), 0, 0, geo.width(), line)

    for i in range(lay.count()):
        item = lay.itemAt(i)
        child_w = item.widget()
        child_lay = item.layout()
        if child_w is not None:
            walk(child_w, f"{path}/{i}:{type(child_w).__name__}", depth + 1, out_lines, nodes)
        elif child_lay is not None:
            describe_layout(child_lay, f"{path}/{i}:{type(child_lay).__name__}",
                             depth + 1, out_lines, nodes)


# Widgets we deliberately do not descend into (irrelevant/huge subtrees:
# the Browser/thumbnail grid, popups, etc.) — still printed, not expanded.
_LEAF_TYPES = (QLabel, QPushButton)


def walk(w: QWidget, path: str, depth: int, out_lines: list, nodes: dict) -> None:
    describe_widget(w, path, depth, out_lines, nodes)

    if isinstance(w, _LEAF_TYPES):
        return  # leaf — do not descend further

    if isinstance(w, QScrollArea):
        inner = w.widget()
        if inner is not None:
            walk(inner, path + "/viewport-widget", depth + 1, out_lines, nodes)
        return  # layout() is None for QScrollArea itself; handled explicitly

    # Already recursed via layout() inside describe_widget for the common
    # case. Widgets with children NOT reachable via layout() (rare in this
    # codebase) would be missed — none of Inspector/TabbedPanel/TabPanel/
    # CollapsibleSection/MetadataBlock fall into that category here.


def dump_stage(win: IllustrationWindow, stage: str) -> dict:
    out_lines = [f"\n{'=' * 100}\nSTAGE: {stage}\n{'=' * 100}"]
    nodes: dict = {}

    walk(win, "Window", 0, out_lines, nodes)

    splitter = win._splitter
    out_lines.append(f"\n-- splitter.sizes() = {list(splitter.sizes())} --")

    inspector = win._inspector_shell
    out_lines.append("\n-- Inspector subtree --")
    walk(inspector, "Inspector", 0, out_lines, nodes)

    print("\n".join(out_lines))
    return nodes


_LEAF_TYPES_NAMES = {"QLabel", "QPushButton", "HoverIconButton", "QComboBox"}


def diff_stages(prev: dict, cur: dict, prev_name: str, cur_name: str) -> None:
    print(f"\n{'-' * 100}\nDIFF: {prev_name}  ->  {cur_name}\n{'-' * 100}")
    first_any = None       # first node in traversal order whose own sizeHint/minimumSizeHint grew
    first_leaf = None      # first LEAF (QLabel/QPushButton/QComboBox) among those — the actual
                           # content-originated cause, since ancestor containers necessarily show
                           # the same growth *after* Qt propagates a leaf's changed sizeHint
                           # upward during the same layout pass (a static before/after snapshot
                           # cannot distinguish "cause" from "propagated effect" any other way).
    any_change = False
    for path, node in cur.items():
        old = prev.get(path)
        if old is None:
            continue
        grew_sizehint = node.size_hint_w > old.size_hint_w
        grew_minhint = node.min_size_hint_w > old.min_size_hint_w
        grew_width = node.width > old.width
        if grew_sizehint or grew_minhint or grew_width:
            any_change = True
            marker = "  <-- LEAF (content-originated)" if node.cls in _LEAF_TYPES_NAMES and (grew_sizehint or grew_minhint) else ""
            print(
                f"  WIDTH GREW: {path}  [{node.cls} name={node.name!r}]{marker}\n"
                f"    sizeHint.w        {old.size_hint_w:>5} -> {node.size_hint_w:<5}\n"
                f"    minimumSizeHint.w {old.min_size_hint_w:>5} -> {node.min_size_hint_w:<5}\n"
                f"    width()           {old.width:>5} -> {node.width:<5}"
            )
            if (grew_sizehint or grew_minhint) and first_any is None:
                first_any = (path, node)
            if (grew_sizehint or grew_minhint) and node.cls in _LEAF_TYPES_NAMES and first_leaf is None:
                first_leaf = (path, node)
    if not any_change:
        print("  (no widget width increased between these two stages)")
    if first_any is not None:
        path, node = first_any
        print(f"\n  >>> FIRST node in top-down traversal order whose sizeHint/"
              f"minimumSizeHint width grew (may be a container reflecting a "
              f"descendant's change): {path}  [{node.cls} name={node.name!r}]")
    if first_leaf is not None:
        path, node = first_leaf
        print(f"  >>> FIRST LEAF widget (QLabel/QPushButton/QComboBox) whose own "
              f"sizeHint/minimumSizeHint width grew — the actual content-driven "
              f"origin: {path}  [{node.cls} name={node.name!r}]")



def main() -> None:
    import tempfile

    app = QApplication.instance() or QApplication(sys.argv)

    # NOTE: `tool.prefs.get("window_illustration_panel_sizes", ...)` etc. are
    # resolved against whatever project is remembered in the real, global
    # ~/.crossing/prefs.json "path" key — NOT the throwaway project_dir this
    # script constructs below. On this machine that real project already has
    # a persisted (likely stale, pre-dating any fix) wide inspector size on
    # disk, which would otherwise contaminate stage 1 (startup) with a
    # restored width instead of a fresh natural fit. To get a clean,
    # reproducible baseline for this read-only diagnostic run only (no files
    # are written, nothing is "fixed"), the illustration-specific keys are
    # neutralized in-process for the lifetime of this script.
    from tool import prefs as _prefs
    _real_get = _prefs.get
    _neutralized_keys = {
        "window_illustration_panel_sizes", "window_illustration_browser_mode",
        "window_illustration_fullscreen", "window_illustration",
    }

    def _patched_get(key, default=None):
        if key in _neutralized_keys:
            return default
        return _real_get(key, default)

    _prefs.get = _patched_get

    project_dir = tempfile.mkdtemp(prefix="crossing_layout_probe_")
    win = IllustrationWindow(project_dir)
    win.show()
    app.processEvents()
    app.processEvents()

    stages = {}
    stages["1_startup"] = dump_stage(win, "1) STARTUP (empty catalog, no selection)")

    # ---- Stage 2: "catalog finished loading" ---------------------------------
    # Inject fake catalog items purely in-memory (monkeypatch the source's
    # items() for this diagnostic run only — no files/production code changed)
    # and ask the browser to refresh its filtered view from that data.
    cat = win._catalog
    fake_items = [
        {
            "label": "man-standing-in-doorway-holding-rifle-looking-left",
            "filename_stem": "IlBuonoIlBruttoIlCattivo_1966_TheGoodTheBadAndTheUgly",
            "filename": "IlBuonoIlBruttoIlCattivo_1966_TheGoodTheBadAndTheUgly.mkv",
            "shot_id": "sh_00042_0000123456789",
            "frame": 12345,
            "sam_model": "sam3.pt",
            "confidence_score": 0.912,
            "human_best": False,
            "mask_area": 5000,
            "frame_size": [1920, 800],
            "bbox": [10, 10, 200, 300],
            "path": str(Path(project_dir) / "fake_object.json"),
        }
    ]
    cat._sil_source.items = lambda: list(fake_items)
    cat._browser_sil.refresh()
    app.processEvents()
    app.processEvents()
    stages["2_catalog_loaded"] = dump_stage(win, "2) CATALOG LOADED (fake items injected)")

    # ---- Stage 3: "first object selected" ------------------------------------
    cat._on_selection_changed(fake_items[0])
    app.processEvents()
    app.processEvents()
    stages["3_object_selected"] = dump_stage(win, "3) OBJECT SELECTED")

    # ---- Stage 4: "inspector manually resized wider" --------------------------
    splitter = win._splitter
    total = sum(splitter.sizes())
    wider_inspector = min(total - 100, max(splitter.sizes()[1] + 400, 700))
    splitter.setSizes([max(100, total - wider_inspector), wider_inspector])
    app.processEvents()
    app.processEvents()
    stages["4_inspector_widened"] = dump_stage(win, "4) INSPECTOR MANUALLY WIDENED")

    # ---- Diff report ------------------------------------------------------
    order = ["1_startup", "2_catalog_loaded", "3_object_selected", "4_inspector_widened"]
    print(f"\n\n{'#' * 100}\nDIFF REPORT (stage-to-stage)\n{'#' * 100}")
    for a, b in zip(order, order[1:]):
        diff_stages(stages[a], stages[b], a, b)

    win._ipc_server.stop()
    win._ipc_server.wait(500)

    _prefs.get = _real_get

    import shutil
    shutil.rmtree(project_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
