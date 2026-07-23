# Book Inspector Layout Investigation

## Objective

A persistent thin grey strip appeared in the Book Visualizer's inspector area when the internal engraving/browser pane was collapsed. The visible inspector content measures 270 px, but the outer `GripSplitter` and its parents reported a larger allocated width. The objective was to instrument the UI to locate the owning widget or layout responsible for the extra reserved space without changing layout behavior.

## What was investigated

- Book visualizer: added transient instrumentation around inspector construction, internal `GripSplitter` creation, control-panel wrapper, and the collapse/expand flows to capture sizes, geometry, and size-hint aggregation.
- WindowVisualizer: inspected the outer splitter chain, layout margins, and the inspector shell's reported size hints and minimums.
- GripSplitter: inspected the `GripSplitter` class and its handle (`_GripHandle`) to determine whether splitter subclassing altered Qt's sizing or saved/restored state behavior.
- Control panel wrapper: verified that the control panel container is created with an explicit fixed width of 270 px (`setFixedWidth(_PANEL_WIDTH)`), and recorded its sizeHint/minimumWidth behaviour.
- sizeHint / minimumSizeHint aggregation: probed `.sizeHint()`, `.minimumSizeHint()`, and children size/min constraints to understand how nested splitters compute aggregate hints.

## What we ruled out

- The control panel's fixed width (270 px) is intentional and set by the visualizer (`setFixedWidth`). This is not a bug.
- `WindowVisualizer` itself is not inventing an arbitrary inspector width; it uses the inspector shell's `sizeHint()` / `minimumSizeHint()` when fitting the splitter and respects saved splitter sizes.
- `GripSplitter` does not override Qt's splitter sizing or state persistence methods (it only customises the handle via `createHandle`). It does not change `setSizes`, `saveState`, `restoreState`, `sizeHint` aggregation, or `minimumSizeHint` logic.
- The extra reserved pixels come from aggregated sizeHint / minimumSizeHint math: a child's fixed minimum width (270) contributes to the parent's minimumSizeHint along with the sibling sizeHints and the handle width, producing a larger reported minimum than the visible collapsed content.

## Current understanding

After targeted instrumentation and multiple headless runs, the stack traces and numeric decomposition consistently show:

- The internal panel splitter reports `splitter.sizes()` like `[0, 270]` when collapsed, and its child geometry places the visible 270 px control panel at an X offset inside the inspector region (the grey strip is the area outside the child's visible geometry but inside the parent allocation).
- sizeHint/ minimumSizeHint aggregation uses child `sizeHint().width()` plus any child's `minimumWidth()` values and the handle width; this gives the outer splitter a larger `minimumSizeHint()` than the right-pane's current visible width.
- The handle (`_GripHandle`) stores a `_saved_size` for quick collapse/restore operations and toggles pane sizes via `setSizes(...)`, but this behaviour itself does not change how Qt aggregates size hints.

There remains architectural ambiguity about how best to present a collapsed inspector while keeping parent splitters' minimumSizeHint consistent with the visible content; the current implementation computes hints conservatively from child minimums and sizeHints, which leads to the observed extra allotment.

## Decision

We are intentionally stopping the investigation here. No layout changes or speculative fixes will be applied to the Book Visualizer at this time.

Rationale:
- Further changes risk regressing UI parity across visualizers.
- The correct long-term solution is architectural: introduce a shared Inspector abstraction so all visualizers report consistent size hints and layout behaviour.

## Next architectural step

When the project addresses a broader refactor, the recommended next steps are:

- Add a common `Inspector` base abstraction that standardises how inspector widgets compute and report `sizeHint()` and `minimumSizeHint()`.
- Centralise inspector layout policies (e.g. explicit collapsed/expanded semantics, minimum vs preferred widths) so Book, Illustration, Frame Match, and other visualizers derive from the same behaviour.
- Move collapse/restore bookkeeping into the common Inspector class (instead of per-visualizer handle logic) so saved sizes and collapse semantics are consistent.

This file documents the instrumentation findings and the rationale for pausing further changes. The repository has been restored to its pre-investigation runtime state (debug instrumentation removed). If you want, I can open a short follow-up PR that introduces the `Inspector` base scaffold for discussion and iterative implementation.
