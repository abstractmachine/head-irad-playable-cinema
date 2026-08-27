# Archive

These scripts are historical, one-time tools kept for reference. They are **not**
part of the normal `crossing` workflow — nothing in `cli.py`, the MCP server,
the visualizers, or the test suite imports or calls them.

## Migration scripts (one-time data migrations that have already run)

- `migrate_engraving_modes.py` — renamed legacy engraving mode folders
  (`silhouette/` → `isolated/`, `full/` → `frame/`) to the canonical names
  introduced in July 2026.
- `migrate_media_type_folders.py` — renamed the legacy `movies/` folder
  layout to the canonical `movie/` layout introduced in Crossing 2.0.
- `migrate_motifs.py` — moved per-shot motifs from annotation JSONs into
  per-movie motif files.
- `repair_annotations.py` — one-time cleanup of malformed label items in
  annotation JSON files (comma-joined / quote-wrapped labels).

Each of these is idempotent and safe to re-run against an older project
snapshot if you ever need to (see each file's own `Usage` docstring for
flags such as `--dry-run` and `--project`), but they should not need to run
again against a current project.

## Debug probes (throwaway diagnostic tools)

- `debug_illustration_filter_row_probe_v3.py` — one-off width-leak probe for
  the Illustration visualizer's filter row.
- `debug_illustration_inspector_layout.py` — one-off layout instrumentation
  for the Illustration inspector.

## Read-only forensic audits

- `crossing index silhouette morphology-audit` — current read-only historical
  silhouette morphology audit for singular/plural ambiguity. It consumes the
  completed `outputs/tests/silhouette-number-audit/` report and writes its own
  report under `outputs/tests/silhouette-number-morphology-audit/` without
  mutating canonical project state.
- `audit_silhouette_number.py` — older historical precursor to the morphology
  audit. It writes reports under `outputs/tests/silhouette-number-audit/` and
  does not mutate canonical project state.

Both are read-only/diagnostic-only and made no permanent changes to
application behavior when they were used.

## Why archive instead of delete

Deleting a migration script removes the ability to re-run it against an
older project snapshot, and deleting a debug probe loses a working example
of how to instrument the visualizer stack for a similar bug in the future.
Archiving keeps them out of the active `scripts/` workflow while preserving
that history.
