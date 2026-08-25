# Crossing Tool — Internal Engineering Guide for AI Coding Agents

This is **not** user documentation (see `readme.md`/`documentation/` for that). It is an
internal handbook for future AI coding agents (Claude, ChatGPT, Gemini, Codex, Hermes,
etc.) working in this repository. Its purpose is to prevent architectural drift and
duplicated work by explaining what already exists, what's already decided, and what
should never be reinvented.

This guide is derived from the actual codebase (verified by direct inspection, not
assumption) as of 2026-08. Treat `documentation/source.md` with suspicion — it is stale
(references a flat `services/`/`data/` layout and root-level `crossing_mcp.py`/`prefs.py`
files that no longer exist). This guide + direct repo inspection supersede it.

---

## 1. Philosophy

Crossing is a film/gameplay-footage analysis tool with four layers, each a genuinely
separate interface over the *same* underlying capability — none of them should ever
contain business logic the others don't share:

```
CLI (cli.py)  ──┐
Visualizers   ──┼──►  services/  +  generators/  ──►  data/  ──►  <project>/ on disk
MCP server    ──┘         (algorithms,              (canonical
                            rendering)                state I/O)
```

- **CLI is canonical.** `cli.py` is the reference implementation of every capability.
  If a feature doesn't exist as a CLI command, it generally shouldn't be invented
  fresh inside a visualizer or the MCP server.
- **Visualizers are inspectors/editors over CLI functionality**, not a parallel
  implementation. A visualizer button click should call the same `data.*`/`services.*`
  function a CLI command calls — never a reimplementation of that logic in Qt code.
- **MCP exposes a curated, safety-limited subset of the same capabilities** to LLM
  clients (Claude Desktop, etc.) — read-only tools plus a handful of generation tools
  that only ever write to `outputs/`. It must never contain business logic of its own;
  it calls `data.*`/`services.*` exactly like the CLI does.
- **`data/` is canonical state I/O.** It owns the on-disk schema and read/write
  functions for project state (metadata, shotlists, annotations, motifs, palettes,
  books). Everything else treats these functions as the single source of truth.
- **`services/` implements algorithms/workflows** that read canonical data (via lazy,
  function-local imports of `data.*`, almost never top-level imports) and write derived
  caches or trigger heavier pipelines (CLIP/SAM3/TransNetV2/FLUX/OpenAI).
- **`generators/` are pure renderers.** They consume already-computed data (annotations,
  motifs, palettes, search results) and produce PDFs/images. They never write back to
  `data/` — only to `outputs/`. They may call into `data.*`/`services.*` to *read*, but
  are themselves only ever called from `cli.py`, `visualizers/`, or `mcp_server/`.
- **No ORM, almost no dataclasses/schemas.** Nearly all cross-module data is plain
  dicts with implicit, docstring-documented schemas. The only real dataclasses are
  `data/subtitles.py::Cue`, `services/sync_frame_match.py::FrameCatalog`, and
  `generators/mosaic.py::MosaicItem`. Don't expect (or invent) a canonical `Shot`/
  `Frame`/`Silhouette` class — read the relevant module's docstring for the dict shape.
- **Staged, incremental change is the house style.** Every major migration in this
  repo's history (visualizer framework adoption, atomic-write rollout, duplication
  cleanups) was done as a series of small, verified, one-group-at-a-time passes with
  tests + full-suite verification after each step — never a single big-bang rewrite.

---

## 2. Repository map

| Directory | Responsibility |
|---|---|
| `cli.py` | Canonical CLI — argparse dispatch + orchestration only, ~10k lines, no business logic of its own (see §8). |
| `data/` | Canonical project-state I/O: metadata, shotlists, annotations, motifs, film titles, palettes, books, media IDs, subtitle parsing (see §7). |
| `services/` | Processing pipelines/algorithms operating on `data/` state: search, CLIP/SAM3 pipelines (frame matching, silhouette extraction/curation/scoring), engraving generation (FLUX + OpenAI backends), transcode/audio, vocabulary indexing, scene detection, notifications, model management. |
| `generators/` | Pure renderers producing PDFs/images from already-computed data: `cloud.py` (word clouds), `mosaic.py` (contact sheets), `flipbook.py` (per-shot motif/palette book), `composition.py` (single-frame tableau), `palette.py` (color swatch sheet), `_common.py` (shared font-loading helper). |
| `visualizers/` | PyQt5 desktop apps — one `<name>_visualizer.py` per subcommand (book, cloud, flipbook, illustration, metadata, mosaic, palette, project, segmentation, shot, sync). All subclass `WindowVisualizer` (see §3). `launcher.py`/`_window_helpers.py` own cross-window launch/raise/singleton logic. |
| `visualizers/components/` | The shared framework toolkit (Inspector, TabbedPanel, MetadataBlock, ZoomManager, etc. — full inventory in §6). |
| `styles/` | `theme.py` (colors/fonts/spacing tokens + `apply_theme()`, `GripSplitter`, `JumpScrollBar` — see §5), `fonts/` (bundled Hanken Grotesk / Libre Clarendon / Geist / Roboto families), `icons/`. |
| `mcp_server/` | `mcp_server.py` — FastMCP tool definitions exposing a curated subset of `data`/`services`/`generators` to LLM clients (see §9). |
| `tool/` | `prefs.py` (JSON prefs store, user- vs project-scoped, atomic writes), `shortcuts.py` (central keyboard-shortcut constants + `KeyboardManager` + `VisualizerWindow`), `helpers.py` (shared argparse flag builders). |
| `documentation/` | User-facing docs (commands, MCP tool reference, per-visualizer guides, project folder layout). **`source.md` is stale — do not trust it.** |
| `tests/` | ~36 files, mixed `unittest.TestCase` and plain pytest-function style (see §10). |
| `scripts/` | One-time migrations that already ran, plus throwaway debug probes — candidates for archival, not part of the running app. |
| `preferences/styles/` | User-created Cloud word-cloud style presets (JSON), managed by `generators/cloud.py`'s `create_style`/`rename_style`/`delete_style`. |
| `build/`, `crossing.egg-info/` | Packaging artifacts. **Never edit anything under `build/`** — it contains stale, deeply-nested duplicate copies of `visualizers/`/`styles/` from old packaging runs; grepping it produces false hits. Always scope searches to the real top-level directories. |

---

## 3. Canonical architecture (visualizer framework)

Every visualizer in the app (all of book/cloud/flipbook/illustration/metadata/mosaic/
palette/project/segmentation/shot/sync) is a `WindowVisualizer` subclass — this
migration is **complete**, not in progress. The shell hierarchy is:

```
WindowVisualizer (visualizers/window_visualizer.py)
  └─ Inspector (components/inspector.py)
       └─ TabbedPanel / Tab (components/tabbed_panel.py)
            └─ TabPanel (components/tab_panel.py)
                 └─ CollapsibleSection (components/collapsible_section.py)
                      └─ MetadataBlock (components/metadata_block.py)
                           └─ InspectorValue (components/inspector_value.py)
```

- **`WindowVisualizer`** — the shared window shell. Provides a `GripSplitter` of
  `[Browser | optional side panel(s) | Inspector]`, geometry/fullscreen/panel-visibility
  persistence keyed by a `pref_key` string, and Tab/Shift+Tab/Escape/Ctrl+Q/Ctrl+W
  handling. A subclass implements `create_browser()`, `create_inspector()`, optionally
  `create_side_panels() -> list` (or the older singular `create_side_panel()`), and
  optionally `focus_target()`.
- **`Inspector`** — always owns a `TabbedPanel` (even a single-tab inspector uses one,
  for stylesheet consistency). Participates in scrollbar-gutter width reservation.
- **`TabbedPanel`/`Tab`** — only the *active* tab's content is ever parented; hidden
  tabs are fully unparented (not just `.hide()`n) so their content can never inflate
  `sizeHint()`/leak into the visible tab's layout. Note the one caveat documented in
  repo history: a tab's *first-ever* mount reflects whatever size its content happens
  to be at that moment — if a background load finishes for a not-yet-visited tab
  before it's ever shown, its first switch-to can cause a one-time size jump.
- **`TabPanel`** — the actual scrollable content host for one tab; wraps children in a
  `QScrollArea` that does **not** propagate embedded content's `sizeHint()` upward
  (by design — this is what keeps Filter/Sort/Info/Tools sections from ever being able
  to widen the Inspector pane).
- **`CollapsibleSection`** — arrow-header collapsible box, `add_widget()`, persists
  expanded/collapsed state via its own `pref_key`. Attach a `SweepBar` to a section's
  header via `set_subbar()` to show a loading indicator even while collapsed.
- **`MetadataBlock`** — fixed key/value grid for "Info"-style sections; also exposes
  `InspectorTable` for interactive row tables. Value cells are always `InspectorValue`,
  never a raw `QLabel`.
- **`InspectorValue`** — the canonical "a value inside an Inspector" widget: word wrap,
  character-level wrap via zero-width-space interleaving (so even unbroken strings like
  raw filenames wrap instead of forcing width), selectable text. Do **not** add
  domain-specific formatting (clipping/ellipsis) to this class — callers format the
  string themselves before calling `.setText()`.

**Other load-bearing shared pieces** (see full inventory in §6): `KeyboardManager`
(`tool/shortcuts.py`) installs a single app-level Tab/Shift+Tab/Escape/F1–F12 event
filter — do not write a second one. `ZoomManager` is the shared zoom-state holder used
by every visualizer with Ctrl+wheel/Ctrl+Plus/Minus zoom. `SelectionManager`,
`ThumbnailManager`/`ThumbnailLoader`, `AspectGridWidget`, `ThumbnailCell` back the
grid/browser surfaces. `singleton_guard.py` + `ipc_server.py` handle cross-*process*
single-instance enforcement and navigation (most visualizers are cross-process
guarded via `singleton_guard`; Illustration and Shotlist are self-managed via their
own Unix-socket IPC server and are excluded from the generic guard —
`SELF_MANAGED_SUBCOMMANDS` in `singleton_guard.py`).

**One deliberate architectural exception**: `sync_visualizer.py`'s Inspector is a
**floating overlay**, not a splitter pane — the Sync canvas must visually extend
underneath it. This required Sync-local code (`_make_inspector_overlay()`,
`_InspectorGripHandle`, a duck-typed `_InspectorWidthBridge` for the scrollbar gutter)
because `WindowVisualizer` is deliberately kept generic. Don't treat this as a second
canonical pattern — it's a one-off, documented exception for one visualizer's specific
visual requirement.

**When NOT to use this hierarchy**: a genuinely non-inspector splitter pane (e.g. a
video/table side panel with no scrollable-tab structure) is a plain widget added via
`create_side_panels()`, not forced into a `TabPanel`. An editable table
(`QTableWidgetItem`-based, needs in-place double-click editing) is correctly left as a
raw `QTableWidget`, not converted to the read-only `MetadataBlock`/`InspectorValue`.

---

## 4. Creating a new visualizer

Canonical recipe (every visualizer in the repo follows this shape now):

1. `class MyVisualizerWindow(WindowVisualizer):` — construct with
   `super().__init__(pref_key="window_my_thing")`. The `pref_key` alone gives you
   geometry, fullscreen, and panel-visibility persistence — do not hand-roll
   `save_window_geometry()`/`restore_window_geometry()` calls yourself, and do not
   invent a new prefs-key naming scheme (`window_<name>` is the convention).
2. Implement `create_browser()` — return the primary content widget/canvas.
3. Implement `create_inspector()` — build one or more `TabPanel()`s, add
   `CollapsibleSection`s via `panel.add_section(title, widget, pref_key=...)` (or
   `add_widget()` if you already have a full `CollapsibleSection`), wrap in
   `Inspector()` via `inspector.add_tab(panel, "Title")`.
4. Reuse `MetadataBlock` for any key/value "Info" section — don't build a `QGroupBox`
   full of manually-placed `QLabel`s.
5. Reuse `theme.action_button_stylesheet()` for every Inspector action button, and
   `combo_popup.style_canonical_combo(combo)` for every `QComboBox` (sets
   `AdjustToMinimumContentsLength` so long item text can't inflate the Inspector's
   width, plus the themed bold popup `QListView`).
6. `KeyboardManager` is installed automatically — you get Tab (toggle inspector),
   Shift+Tab (toggle fullscreen), Escape/Ctrl+Q/Ctrl+W (close), and F1–F12
   (switch visualizer) for free. Do not write your own `keyPressEvent`/`eventFilter`
   branches for these; only add visualizer-specific bindings on top.
7. Register a cross-process singleton guard if the visualizer is normally launched
   both in-process (via `switch_to_visualizer()`/Project hub) and standalone
   (`crossing visualizer <name>`) — call `claim_or_ping_and_bind()` from
   `singleton_guard.py` inside your `run_visualizer()`, unless the visualizer already
   has its own IPC server (then add it to `SELF_MANAGED_SUBCOMMANDS` instead).
8. Use `launcher.run_visualizer_window(subcommand, build_window, ...)` as your
   `run_visualizer()` body — it already handles "reuse existing `QApplication`",
   `apply_theme()`, `.show()`, `sys.exit(app.exec_())`, in one shared helper.
9. Add the visualizer to `visualizers/_window_helpers.py`'s title map and
   `project_visualizer.py`'s `_create_in_process_window()` dispatch table so F-key
   switching and the Project hub's launcher buttons can find it.

**Avoid**: a bespoke `QMainWindow` shell, a hand-rolled `QTabWidget`+`QScrollArea`
inspector, a private copy of combo-popup styling, a private `keyPressEvent`
reimplementing Tab/Shift+Tab/Escape, or a new geometry-persistence pref key scheme.
Every one of these has been built once already and later migrated back onto the
shared framework at real cost — check `visualizers/components/` and `styles/theme.py`
before writing anything that "feels shared."

---

## 5. Shared UI principles

- **Tab** — hide/show the Inspector (and any side panels, if the visualizer has them).
- **Shift+Tab** — toggle real OS fullscreen (`showFullScreen()`/`showNormal()`).
- **Escape** — close the window (respect any existing "discard a dirty edit first"
  widget-level special case only if it already existed before your change — the
  app-level `KeyboardManager` intercepts Escape first, so a widget-level Escape
  handler generally cannot run before it).
- **F1–F12** — switch between visualizers (`tool/shortcuts.py`'s
  `FUNCTION_KEY_BINDINGS`).
- **Inspector organization**: `CollapsibleSection`s, most-used-first, each with its own
  `pref_key` for expand/collapse persistence. A section that's loading in the
  background gets a `SweepBar` on its title (`section.set_subbar(bar)`), started/
  stopped around the async operation — in **every** completion path (success AND
  error), not just success.
- **Large widget-list population**: a worker can make data retrieval cheap while the
  GUI still freezes constructing thousands of row widgets. Use Mosaic's
  `VocabularyTable` pattern for size-to-content Inspector lists: fetch and prepare
  data in a worker; create rows in bounded `QTimer` batches so the event loop keeps
  running; while batching, hide only the table and disable its updates/layout; then
  re-enable layout and reveal the completed table exactly once. Keep navigation and
  the section-header `SweepBar` visible throughout. This avoids an outer `TabPanel`
  scroll-area relayout/repaint after every batch — the reason collapsing a section
  can otherwise make the same population appear dramatically faster. Do not apply
  this blindly to virtualized item views (`QListView`/`QTableView`), which should use
  their model APIs instead of constructing one QWidget per row.
- **Metadata tables** use `MetadataBlock`, not ad-hoc `QLabel` grids.
- **Colors/spacing/typography are always tokens from `styles/theme.py`**, never
  hardcoded hex/pixel literals: `BG`/`PANEL_BG`/`TAB_BG`/`CELL_BG`/`CANVAS_BG`
  (backgrounds), `TEXT`/`TEXT_DIM`/`ACCENT`/`ACCENT_TEXT` (text/selection),
  `BTN_BG`/`BTN_HOVER`/`BTN_PRESSED`/`BTN_H` (buttons), `SECTION_GAP`/`INSPECTOR_GAP`
  (spacing — these are aliases of each other), `font_ui()`/`font_mono()`/
  `font_subtitle()` (Geist for UI chrome, Geist Mono for data/info fields).
- **Scrollbars — read the developer-note comments in `theme.py` before touching
  anything scrollbar-related.** `SCROLLBAR_W` is the container's fixed footprint and
  must **never** change between idle/hover/active/pressed states (a real regression
  was shipped and reverted for exactly this reason) — only the *handle*'s color/border
  may change on hover. Idle handle color is `SCROLLBAR_IDLE_COLOR` (aliased to
  `TAB_BG`, so it always matches tab/panel chrome automatically).
- **Combo boxes never wrap** (no Qt API for it) — always call
  `combo_popup.style_canonical_combo(combo)` rather than trying to make a combo itself
  show full wrapped text; if the full value must be visible, show it in a separate
  `QLabel` with `setWordWrap(True)` next to the combo.

---

## 6. Existing reusable components (`visualizers/components/`)

| Component | Purpose | Reuse when… |
|---|---|---|
| `inspector.py` (`Inspector`) | Owns a `TabbedPanel`; scrollbar-gutter participant | Any visualizer's Inspector pane |
| `tabbed_panel.py` (`TabbedPanel`/`Tab`) | Tab strip + single-active-page host | Any tabbed inspector or browser |
| `tab_panel.py` (`TabPanel`) | Scrollable content host for one tab | Content of one Inspector tab |
| `collapsible_section.py` (`CollapsibleSection`) | Arrow-header collapsible box | Any Inspector section |
| `metadata_block.py` (`MetadataBlock`, `InspectorTable`) | Key/value grid / interactive row table | Any "Info"/attribute display |
| `inspector_value.py` (`InspectorValue`) | Canonical wrapping value `QLabel` | Any `MetadataBlock` value cell (automatic) |
| `combo_popup.py` (`attach_combo_popup`, `style_canonical_combo`) | Themed popup `QListView` + width-safe sizing | Every `QComboBox` |
| `zoom_manager.py` (`ZoomManager`) | Clamped zoom-level state + persistence + wheel/key handling | Any Ctrl+wheel/Ctrl+±/0 zoom feature |
| `selection_manager.py` (`SelectionManager`) | Browser selection state/visuals/signals | Any grid/list single-selection browser |
| `thumbnail_manager.py`/`thumbnail_loader.py` | Background thumbnail loading off the GUI thread | Any thumbnail grid |
| `thumbnail_cell.py` (`ThumbnailCell`) | Grid item widget with click/double-click/selection border/drag-drop | Any thumbnail grid item |
| `aspect_grid.py` (`AspectGridWidget`) | Uniform-aspect-ratio grid layout | Any same-size-cell grid |
| `sweep_bar.py` (`SweepBar`) | 2px sweeping accent-color loading indicator | Any section-header loading state |
| `scrollbar_gutter.py` (`ScrollbarGutter`) | Reserves `SCROLLBAR_W` when a tab's scrollbar appears | Automatic via `Inspector`; don't invoke directly unless building a non-standard host |
| `hover_icon_button.py` (`HoverIconButton`) | Icon-swap-on-hover/checked button | Any icon toggle button |
| `flow_widget.py` (`FlowWidget`) | Deterministic flow (wrap) layout | Any browser needing a flowing grid, not a fixed grid |
| `side_panel.py` (`SidePanel`) | Independent, non-Inspector splitter pane | A second fixed-width pane beside Browser/Inspector (e.g. Book's Engravings catalog) |
| `singleton_guard.py` | Cross-process single-instance claim-or-ping | Any standalone-launchable visualizer without its own IPC server |
| `ipc_server.py` (`IpcServer`) | Reusable Unix-socket server base | Cross-process navigation (Illustration, Shotlist; subclass, override `_handle_message`) |
| `illustration_browser.py`/`illustration_source.py`/`illustration_inspector.py` | Generic collection browser/source/inspector abstractions | Used by both Book and Illustration despite the name (misleading name, known debt — see §12). `illustration_inspector.py`'s `IllustrationInspector` class is **dead code**, never instantiated — do not build on it. |

**When to create a new component instead of reusing**: only when the interaction
pattern is genuinely novel (e.g. Sync's node-graph canvas, drag/resize/cable-connect —
nothing else in the app does this) — and even then, check whether an existing
primitive (e.g. `GripSplitter`'s grip-dot paint routine, reused as a *lookalike*
standalone widget for Sync's floating-overlay grip handle) can be copied at the
paint/behavior level without forcing a structurally incompatible base class.

---

## 7. Data architecture

- **Canonical data** (source of truth, written directly by CLI commands / user
  actions, via `data/`):
  - `data/metadata.py` → `data/metadata/{movies,gameplay}.json` (title/year/tmdb/etc.)
  - `data/shotlist.py` → `data/shotlists/<type>/<file>.csv` (shot boundaries/timing)
  - `data/annotate.py` → `data/annotations/shots/<type>/<file>.json` (per-shot LLM
    annotations; also where `data/motif.py` writes each shot's `motif` field back into
    the *same* file — annotation JSON has authoritative writers spread across at least
    4 files: `data/annotate.py`, `services/frame_match.py`,
    `visualizers/shot_visualizer.py`, `data/motif.py` — grep
    `get_annotation_json_path` before assuming a migration/audit of this file is
    complete)
  - `data/book.py` + `visualizers/book_visualizer.py`'s 3 sidecars → `outputs/books/<slug>/`
    (`book.json`, `layers.json`, `selections.json`, `mask.json`)
  - `data/film_motif.py` → `data/film_titles/<type>/<file>.json` (per-film semantic title)
- **Derived/cache data** (regenerable from canonical data + a model pipeline):
  `data/palette.py` (`data/palettes/`, SAM3 via the canonical
  `segment_palette(image_pil)` adapter), `services/silhouette*.py` (silhouette cache +
  object catalog), `services/illustration_index.py` (browse index + indexed
  `search_provenance` for silhouette records), `services/silhouette_provenance.py`
  (one-time provenance migration from the completed semantic audit),
  `services/vocabulary_index.py`, `services/frame_embeddings.py`
  (`.npy` + manifest), `services/engraving_*` (PNG/JSON per generation run — cache-like,
  each `engraving_id` unique, no cross-run collision).
- **Writes must be atomic.** `data/annotate.py::atomic_write_text(path, text)` is the
  shared, public helper (temp file in the same directory + `os.replace()`, cleanup on
  failure) — reuse it (lazy-import `from data.annotate import atomic_write_text`) for
  any new canonical JSON/text write. It already covers: `tool/prefs.py`, all
  annotation writers, the silhouette object catalog + manifest, shotlist CSV, book +
  its 3 sidecars, metadata JSON, motif/film-title writers, `data/index.py`'s
  fields.yaml, vocabulary index, and the frame-embeddings manifest. **Binary writes
  (`.npy`, PNG) are correctly left as direct writes** — `atomic_write_text()` only
  applies to text; do not invent a binary-atomic wrapper without an explicit request.
  Regenerable exports (CSV/Markdown reports) and debug logs are also correctly left
  non-atomic.
- **Relationship**: `services/` and `generators/` read canonical `data/` state (via
  lazy, function-local `from data.X import Y` — this is the dominant import style, not
  a stylistic accident: it avoids circular imports and defers heavy/optional model
  loading) and write to caches or `outputs/`. `generators/` almost never import from
  `data/`/`services/` at all besides the reads they need to render — the one confirmed
  exception is `services/frame_retrieval.py` importing `generators.mosaic` for frame
  extraction, a justified, narrow reuse.

---

## 8. CLI philosophy

`cli.py` (~10k lines) is the canonical implementation and **pure orchestration** —
argparse dispatch (`cmd_<group>(args)` functions, each further dispatching to
`_subcmd_*`-style helpers for its own sub-subcommands) that resolves arguments/prefs,
calls exactly one `data.*`/`services.*`/`generators.*` function to do the real work,
and prints/exits. Business logic belongs in `data/`/`services/`/`generators/`, never
in `cli.py` itself. Imports of those modules are almost always **local to the handler
function**, not top-level — follow this convention for new commands.

Representative top-level command groups (not exhaustive — grep `add_parser(` if you
need the full current list): `import`, `media`, `metadata`, `remove`, `shotlist`,
`shot`, `annotate`, `search`, `index`, `generate`, `engraving`, `subtitle`, `api_key`,
`backup`, `book`, `sync`, `notify`, `visualizer`, `tool`.

Visualizers and the MCP server must call the *same* `data.*`/`services.*` functions a
CLI command would — never re-derive the same result a different way. When a
CLI-vs-MCP resolution helper looks similar but has a different failure convention
(CLI: print + `sys.exit(1)`; MCP: return a JSON error string), extract **one helper per
file matching that file's own idiom** — do not force a single cross-file shared
signature (this was tried and explicitly rejected; see `cli.py`'s
`_resolve_single_normalize_match_or_exit` vs. `mcp_server.py`'s `_resolve_single_film`).

---

## 9. MCP philosophy

`mcp_server/mcp_server.py` (FastMCP-based, `from mcp.server.fastmcp import FastMCP`) is
**another interface over the same project data — not a place to implement anything
new.** Every tool calls straight into `data.*`/`services.*`/`generators.*`, using the
same shared `_ctx()` (resolve/validate project path), `_ok(**payload)`/`_err(message,
detail)` response-JSON helpers, and a film-resolution helper
(`_resolve_single_film(project_path, film, media_type) -> tuple[dict, str] | str`)
mirroring `_ctx()`'s own "tuple on success, JSON error string on failure" convention.

**Access policy** (by convention + a shared `_output_dir()` helper, not sandboxed):
tools read freely from `data/`, `media/`, `preferences/`; generation tools write
**only** under `outputs/<subdir>/` (flipbooks, mosaics, clouds, compositions, catalogs).
The generic derived-workspace conventions are `outputs/agent/` for scratch or working
artifacts and `outputs/review/` for reviewable or provisional artifacts; test and audit
artifacts belong under `outputs/tests/` (never `outputs/test/`); do not add a
default path, fallback, or example that resurrects the singular `output/test`
form; these names do
not imply a queue or promotion workflow. No tool ever writes to
`data/annotations/`, `data/shotlists/`, `data/metadata/`, or `preferences/` — anything
destructive or expensive (annotation, motif generation, palette building, shotlist
editing, metadata editing, subtitle download, model management, vocabulary rebuild,
silhouette extraction, embedding index) is **intentionally CLI-only** and must stay
that way unless the user explicitly asks to add it. The silhouette provenance
migration is part of that CLI-only set; it reads the completed audit and writes the
additive `search_provenance` field back into existing silhouette JSON records, then
rebuilds the browse index.

Tools are organized in tiers (see `documentation/mcp.md` for the maintained reference,
though it currently under-documents ~16 of the ~30 real tools — Tier 3 analysis tools
like `compare_motifs`/`search_cooccurrence`/`get_shot_context` and Tier 4 meta tools are
real and callable but missing from the written docs; if you add a new tool, document it
there too instead of letting the gap grow further).

---

## 10. Testing philosophy

- **Run command**: `QT_QPA_PLATFORM=offscreen uv run pytest tests/ -q` (or `python3
  -m py_compile <file>` for a fast syntax check first). `QT_QPA_PLATFORM=offscreen` is
  **not** auto-configured anywhere (no `conftest.py`, no `pytest.ini`/
  `pyproject.toml` setting) — you must export/prefix it manually for any test that
  touches Qt.
- **No `conftest.py` exists.** Fixtures are inline per-file: either plain
  pytest-style `@pytest.fixture` functions (common in visualizer tests — `app`,
  `fake_prefs`, `fake_movie`) or plain leading-underscore helper functions
  (`_make_mask_dict()`, `_make_catalog_entry()`, `_stub_model()`) that build fixture
  data directly, especially in `unittest.TestCase`-style files.
- **Both `unittest.TestCase` classes and plain pytest functions are established,
  accepted conventions** — pick whichever matches the file you're extending, don't
  force a rewrite to "unify" style.
- **Small, focused tests** — one `test_<feature_or_module>.py` file per feature area
  (not strictly 1:1 with source files), one `TestXxx` class per function/behavior
  under test when using `unittest.TestCase`.
- **CRITICAL**: any offscreen test that constructs a *real* visualizer window must
  monkeypatch **both** `tool.prefs.get` **and** `tool.prefs.set` (not just `get`) to an
  in-memory store before construction. A real incident: a throwaway probe script that
  only patched `.get` still called a real `.set()` deep in a `CollapsibleSection`
  toggle and silently corrupted a real project's `preferences/preferences.json` on
  disk. Always sandbox both.
- **Integration-style construction of real Qt windows is normal and encouraged** for
  verifying the shared framework (splitter panes, tab switching, keyboard shortcuts) —
  it is not mocked away. What *is* always monkeypatched: heavy ML pipeline pieces
  (CLIP/SAM3/FLUX/OpenAI/TransNetV2), and always at the **imported name inside the
  module under test** (e.g. `services.frame_embeddings.load_frame_embedding_model`),
  not the underlying model class — most of this repo's modules already do lazy,
  function-local imports specifically to make this monkeypatch point reachable.
- **A pre-existing, known-flaky segfault** can occur roughly 1-in-4 full-suite runs at
  interpreter shutdown inside a background `IpcServer`/`QThread` accept-loop teardown
  race. It is unrelated to most feature work — if a run fails with a bare segfault and
  no test-level traceback, rerun once or twice before treating it as a real regression.
- After any change: run `get_errors` on touched files, `python3 -m py_compile` on
  touched files, then the full suite, and confirm the pass count increased by *exactly*
  the number of tests you added (not more, not fewer) — this repo's established
  verification discipline.

---

## 11. Recent architectural decisions

- **Visualizer framework migration — complete.** Every visualizer (book, cloud,
  flipbook, illustration, metadata, mosaic, palette, project, segmentation, shot,
  sync) is now a `WindowVisualizer` subclass using `Inspector`/`TabbedPanel`/
  `TabPanel`/`CollapsibleSection`. Shotlist (`shot_visualizer.py`, ~3500 lines) was the
  last plain-`QMainWindow` holdout and required generalizing `WindowVisualizer` itself
  with a `create_side_panels() -> list` hook (plural; the old singular
  `create_side_panel()` is a backward-compatible wrapper) to support its two
  independent fixed-width side panes (Scene + Shot tables) alongside Browser+Inspector.
- **`KeyboardManager` centralization** — every visualizer-specific Tab/Shift+Tab/
  Escape event-filter reimplementation has been deleted in favor of one app-level
  filter (`tool/shortcuts.py`).
- **Atomic-write rollout — staged, tiered, complete for all canonical text/JSON
  writes** identified so far (prefs → annotations → silhouette catalog → shotlist/book
  → metadata → motif/film-title/vocabulary-fields → cache-tier vocabulary
  index/frame-embeddings manifest). All reuse the single `data.annotate
  .atomic_write_text()` helper. Binary writes and regenerable exports were
  deliberately left as direct writes (see §7).
- **Shared font-loading helper** (`generators/_common.py`) — `cloud.py`/`flipbook.py`/
  `mosaic.py` each used to redefine an identical font-fallback search chain and
  near-identical `_load_font()`; now all three delegate to
  `load_font_with_fallback(size, preferred_paths=...)`.
- **Engraving prompt-variable parity** — the FLUX backend
  (`services/engraving_generate.py`) was missing the `$motif` canonical placeholder
  that the OpenAI backend (`services/engraving_generate_openai.py`) already supported;
  fixed to match the documented canonical set in `services/engraving_prompt.py`
  (`label`, `field`, `movie`, `shot_id`, `description`, `motif`). Note: FLUX's
  `generate_engraving()` is **not** part of the production batch pipeline
  (`batch_generate_engravings` hardcodes the OpenAI backend) — FLUX is reachable only
  via the manual `crossing engraving smoke-test` CLI command.
- **Cross-process singleton guard** (`singleton_guard.py`) — added after a real
  duplicate-window bug report; `raise_existing_window()` only ever prevented
  duplicates *within one process*, but every visualizer can also be launched as an
  independent OS process. Illustration and Shotlist already had their own bespoke IPC
  and are excluded from the generic guard (`SELF_MANAGED_SUBCOMMANDS`).
- **Composition visualizer removed entirely** (had a pre-existing, never-fixed
  `IndentationError` — could never actually run). The headless `crossing generate
  composition` CLI command and the MCP `generate_composition` tool were **kept** —
  they call `generators/composition.py` directly and are independent of the deleted
  GUI. When asked to "remove visualizer X," always check whether a same-named headless
  CLI/MCP path shares the same generator module before deleting anything underneath it.
- **Sync visualizer's Inspector-as-floating-overlay** — a deliberate, one-off exception
  to the splitter-pane Inspector pattern (see §3); the generic `WindowVisualizer` was
  kept unchanged, all the special-casing lives in `sync_visualizer.py`.

---

## 12. Current technical debt

**Debt (real, confirmed, unaddressed):**
- `cli.py` (~10k lines) and `mcp_server/mcp_server.py` (~2.5k lines) have **zero test
  coverage of their own orchestration layer** — the `data.*`/`services.*` functions
  they call are tested, but the `cmd_*`/tool-decorated functions themselves mostly
  aren't. Biggest test-coverage gap in the repo.
- Bare `except Exception: pass`/`continue` silent-failure pattern is widespread
  (~90+ sites across `data/`+`services/`) — the single most common recurring smell in
  every past audit.
- `documentation/source.md` is stale (references a flat pre-reorganization
  `services/`/`data/` layout and root-level files that no longer exist) — don't trust
  it; this guide + direct inspection are more current.
- `documentation/mcp.md` under-documents roughly half the real MCP tools (Tier 3
  analysis tools and Tier 4 meta tools exist and work but aren't written up).
- A handful of hardcoded hex colors remain in `shot_visualizer.py` (best-frame
  highlight colors) that should be `theme.py` tokens but aren't yet — not urgent, but a
  legitimate small cleanup target if you're already touching that area.
- `visualizers/components/illustration_browser.py`/`illustration_source.py`/
  `illustration_inspector.py` are misleadingly named (generic collection-browser/
  inspector abstractions shared by Book and Illustration, not Illustration-only) —
  `IllustrationInspector` itself is confirmed dead code (zero instantiation sites).
- `scripts/*.py` (one-time migrations that already ran + throwaway debug probes) are
  candidates for archival — not part of the running app, kept for historical reference.
- A pre-existing, intermittent (~1-in-4-runs) interpreter-shutdown segfault in
  background `IpcServer`/`QThread` teardown — not root-caused/fixed, unrelated to most
  feature work (see §10).

**Intentional decisions — do not "fix" these into agreement:**
- `services/silhouette.py` vs `services/silhouette_catalog.py`'s mask-quality area
  fraction thresholds (`_MIN_MASK_AREA_FRACTION`/`_MAX_MASK_AREA_FRACTION`) are
  deliberately different (the catalog pipeline is tuned looser/tighter on purpose,
  documented in-code) — `_BORDER_CHECK_PX`/`_MAX_ASPECT_RATIO` *were* accidental
  duplicates and were deduplicated (catalog now imports them from `silhouette.py`).
- Sync's floating-overlay Inspector is a one-off, not a pattern to generalize into
  `WindowVisualizer`.
- FLUX engraving backend intentionally not wired into the production batch pipeline.

**Future opportunities (no action needed unless asked):**
- Renaming `illustration_browser.py`/`illustration_inspector.py` to something
  media-agnostic (e.g. `collection_browser.py`).
- Writing up the missing Tier 3/4 MCP tool docs.
- Adding CLI/MCP orchestration-layer tests.

---

## 13. Future coding guidelines

- Grep `visualizers/components/` **and** `styles/theme.py` **and**
  `combo_popup.py`-style likely-named files before writing any new "shared-looking"
  helper — a canonical implementation may already exist outside `theme.py` (combo
  popup styling was accidentally reinvented three times before this was learned).
- Never resize a canonical fixed-size UI token (e.g. `SCROLLBAR_W`) for a new visual
  state — express new states via color/border changes to what's drawn *inside* the
  existing footprint.
- No new window shells (always a `WindowVisualizer` subclass) and no new
  geometry-persistence mechanisms (always `pref_key`).
- No hardcoded hex colors — use `styles/theme.py` tokens.
- Reuse `data.annotate.atomic_write_text()` for any new canonical JSON/text write;
  only skip it for binary or genuinely regenerable/cache artifacts, and say why.
- Keep changes incremental and staged — this repo's established working style is a
  series of small, verified passes (one file-group at a time), not a single rewrite.
- Before changing any constant/behavior, grep the *whole* repo (not just the file
  you're editing) for other assumed-fixed usages — several past regressions came from
  changing a "local-looking" constant that 2-3 other files already depended on staying
  fixed.
- When you find duplication, classify it explicitly: **accidental** (same value, same
  intent, drifted apart by copy-paste — extract/reuse a canonical source) vs.
  **intentional** (documented, deliberately different — leave alone, and say why in
  your own summary/comment). Never silently merge intentionally-different values.
- Prefer many small, verified edits over one large edit — verify after each one
  (`get_errors`, `py_compile`, full suite) rather than batching several risky changes
  before checking anything.

---

## 14. Anti-patterns (mistakes already made and fixed once — don't repeat them)

- **Duplicating prompt-variable expansion** across the two engraving backends instead
  of matching the documented canonical variable set in `services/engraving_prompt.py`.
- **Duplicating font-loading fallback chains** across `generators/*.py` instead of a
  shared `generators/_common.py` helper.
- **Duplicating quality-filter thresholds** across the two silhouette pipelines when
  they should be one source of truth (vs. correctly leaving genuinely different
  thresholds alone, with a comment explaining why) — know the difference.
- **Creating a second keyboard event filter/handler** instead of reusing the single
  app-level `KeyboardManager`.
- **Adding another bespoke `QMainWindow` shell** instead of subclassing
  `WindowVisualizer` — every visualizer that once did this was later migrated back at
  real cost.
- **Writing JSON/text directly** (`Path.write_text(json.dumps(...))`) instead of going
  through `atomic_write_text()` for canonical state.
- **Hardcoding a new color** instead of adding/using a `styles/theme.py` token.
- **Duplicating film/media resolution logic** instead of reusing (or, when
  conventions genuinely differ, matching) the existing per-file resolver idiom.
- **Reinventing combo-popup styling** — always check `combo_popup.py` first.
- **Resizing a "canonical, fine-tuned" widget footprint** (e.g. a scrollbar's own
  container width) to express a new hover/active state, instead of only changing what's
  painted inside the unchanged footprint — this shipped as a real, user-reported
  regression once already.
- **Assuming a "hand-rolled duplicate of shared component X" is live code that needs
  migrating** — always grep for `ClassName(` (the *instantiation*, not the `class
  ClassName` definition) repo-wide first; it might be entirely dead code that should be
  deleted outright instead (this has happened more than once, e.g. Book's
  ~420-line `_IllustrationsDrawer` subsystem, which had zero real callers).
- **Deleting a visualizer's GUI without checking for a same-named headless CLI/MCP
  command sharing its generator module** (Composition's headless CLI/MCP path was
  correctly kept when its GUI was removed).

---

## 15. Development checklist

Before considering a change complete:

- [ ] Checked `/memories/repo/` (this session's own repo memory, if using an agent with
      persistent memory) and this `AGENTS.md` for prior art before starting.
- [ ] Grepped for an existing shared implementation (`visualizers/components/`,
      `styles/theme.py`, `data/`, `services/`) before writing a new helper.
- [ ] Preserved existing behavior exactly, except where a change was explicitly
      requested.
- [ ] Classified any duplication found as accidental (extracted/reused a canonical
      source) or intentional (left alone, documented why) — never silently merged.
- [ ] Used `atomic_write_text()` for any new canonical JSON/text write (or explained
      why it doesn't apply — binary/regenerable/cache).
- [ ] Used `styles/theme.py` tokens instead of hardcoded colors/spacing/fonts.
- [ ] Added small, focused tests matching the touched file's existing test style
      (`unittest.TestCase` or plain pytest functions, whichever the file/area uses).
- [ ] Ran `get_errors` on every touched file.
- [ ] Ran `python3 -m py_compile` on every touched file.
- [ ] Ran `QT_QPA_PLATFORM=offscreen uv run pytest tests/ -q` and confirmed the pass
      count increased by exactly the number of new tests added, with zero regressions.
- [ ] If a test constructs a real visualizer window, monkeypatched **both**
      `tool.prefs.get` and `tool.prefs.set`.
- [ ] Kept the change incremental/scoped rather than a big-bang rewrite.
