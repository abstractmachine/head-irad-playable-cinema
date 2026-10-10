# Superseded Silhouette Archive Planning

```bash
crossing silhouette archive --project /path/to/project
```

This command is always a dry run. There is no `--apply`, executor, move,
deletion, lifecycle mutation, identity migration or index rebuild.
It writes only under `outputs/tests/silhouette-archive/` and refuses to
overwrite an existing plan database. It requires the completed verified
`outputs/tests/silhouette-identity-migration/` census. Do not remove retained
evidence merely to rerun the command.

For a versioned rerun that reuses retained authoritative candidates:

```bash
crossing silhouette archive --project /path/to/project --version v2 --revalidate
```

The original evidence is opened read-only; new files go under
`outputs/tests/silhouette-archive/v2/`. Existing versions cannot be overwritten.
Revalidation refuses catalog/index metadata drift, refreshes critical hashes,
asset checks and dependency classification, and does not reread every candidate
JSON. Its candidate state/checksums remain prior evidence, not fresh byte checks.

Stage 1 preparation reporting also checks the installed legacy registry and
describes the tested active-only index transition:

```bash
crossing silhouette archive --project /path/to/project --version v3-preparation --revalidate --preparation
```

This remains dry-run only. It never publishes a registry or rebuilds production
indexes. Its readiness means ready for human archival review, not permission to
move files or execute an unimplemented archive executor.

Explicit `assignment.state = superseded` is rechecked in authoritative JSON.
Active and inactive records are excluded. Missing candidate metadata is a
review case, not an eligible asset. Missing assignments default active through
the existing canonical lifecycle helper.

Proposed mappings preserve the entire hierarchy and legacy names:

```text
data/silhouettes/catalog/movie/Film/label/object_0002.json
archive/silhouettes/catalog/movie/Film/label/object_0002.json
```

`archive-plan.jsonl` contains per-record identity, lifecycle, replacements,
owned files, checksums, dependencies and eligibility. `unique-file-plan.jsonl`
distinguishes records from physical files. `dependency-examples.jsonl` gives
source pointers from the retained census. `report.md`, `consumer-audit.md`,
`summary.json` and the before/after safety evidence describe the findings.
No actual archive directory is created.

## Eligibility

- A: no identified dependency or asset blocker.
- B: current runtime data references a proposed archived asset, including
  literal active-record paths or retained inactive data.
- C: an asset is shared with an active record.
- D: missing, conflicting, unverified or ambiguous evidence requires review.
- E: other historical records or frozen audit reports retain an intentional
  historical dependency. These references are preserved, not rewritten.

Identity-reconstructible `/assignment/supersedes` edges, including index mirrors,
are historical lineage, not runtime image inputs. Consumer tests prove that
active discovery does not follow them and explicit lineage inspection can
recover them after fixture-only archival. Only that precise pointer/kind is
downgraded; literal paths and other dependencies still block movement.
Current indexes retain historical rows; an approved index transition is still
required. Eligibility describes asset dependencies, not deployment readiness.
New Illustration silhouette rebuilds index active records only, using the
existing schema. Old mixed indexes continue active-default queries until the
approved rebuild. `assignment_state=ALL` queries only rows actually present;
it is not a replacement for the explicit archive resolver.

## Active-Only Consumers

Canonical `iter_catalog`/`scan_catalog` default to active records, including
legacy metadata without assignment; malformed assignments are inactive.
Retired and unreadable working records require `include_history=True`.
Engraving best selection, batch/direct generation, catalog PNG generation,
QA preparation, evidence planning and Book insertion enforce canonical state.
Direct consumers reload sidecars instead of trusting stale manifest/index
metadata. Historical `assignment.supersedes` is retained unchanged.

Intentional exceptions are physical catalog audits, number/canonical-search/
provenance forensic audits, lifecycle recheck maintenance, explicitly pinned
benchmark `reference_cases`, and explicit `include_history` evidence/QA
inspection. These do not redefine normal candidate populations. Existing
Book-local image copies and derived preprocessing renders are self-contained
assets rather than fresh catalog selections. Book's browser uses the separate
generated engraving index; its historically named silhouette insertion method
preserves engraving copies and authorizes active state only for silhouette inputs.
Annotation visualizer delegates
to Shotlist and has no silhouette candidate source. Illustration, Book browsing,
CLI discovery and MCP silhouette tools use the active Illustration index.

## Explicit Historical Access

```bash
crossing silhouette inspect-reference data/silhouettes/catalog/movie/Film/coat/object_0002.json \
  --project /path/to/project --include-archive \
  --lookup /path/to/project/outputs/tests/silhouette-archive/v2/plan.sqlite3 --lineage
```

`services.silhouette_archive_reference.resolve_silhouette_reference` accepts an
original path or a legacy `(media_type, filename_stem, label, object_id)` dict.
The default authorizes only active working records; it never searches archive.
Explicit historical access first uses an existing original, then requires an
unambiguous preserved `assets(path,destination,sha256)` lookup. Mapped assets
must preserve the legacy hierarchy, be non-symlink files and match SHA256.
Missing sources, mappings, assets, ambiguous mappings and corrupt assets have
distinct errors. A mapping alone does not mean an archive exists.

`inspect_historical_lineage` resolves immediate predecessors/replacements,
including lists; each missing edge reports its error. Repeated calls inspect
chains without recursive traversal or mutation. Empty replacement sets are
valid retirements. `resolve_historical_report_reference(..., field=...)` reads
a chosen CSV/JSONL row field through the same resolver, preserving the report's
literal old path and bytes. Neither reader rewrites active `supersedes` fields.
Foreign-root absolute paths are rejected rather than guessed.

## Legacy Ordinal Reservations

`services.silhouette_legacy_allocation` implements a transitional per-namespace
`.legacy-ordinal-high-water.json`. Atomic text replacement plus a namespace
lock reserves whole extraction batches; restart/index rebuild cannot reset the
maximum. Failed image writes consume gaps, never permit reuse. The existing
legacy path/ID convention is unchanged and archive is never scanned.

**No production reservation registry is initialized by archive commands.**
Initialization needs approval and must seed every affected namespace from
retained historical maxima before movement. The dry-run reservation JSONL is
only evidence, not an installed registry. If an archive exists and a working
namespace has no marker, allocation fails closed. Removing both archive and
markers destroys that safeguard; they must be preserved as operational state.
Fixture coverage does not establish production deployment; preparation reporting
checks the installed registry and distinguishes deferred execution steps from
implementation blockers.

For atomic production deployment, the same legacy reservation contract can be
stored in one `data/silhouettes/.legacy-ordinal-reservations.sqlite3` registry.
`prepare_registry` stages namespace maxima and counts from the full retained
census and validates current JSON/PNG filename sets and every retained silhouette
file's size/mtime/kind. `publish_registry` revalidates under the existing catalog
lock, refuses overwrite, copies the staged database to a pending registry,
fsyncs it, atomically replaces the final path and fsyncs its directory.
It does not allocate IDs or rewrite existing records. Production deployment
requires explicit authorization; the staged database and publication evidence
remain in a versioned report directory.

The allocator prefers the installed SQLite registry, using `BEGIN IMMEDIATE`
to reserve an entire batch before image writes. New namespaces register their
current maximum in the same transaction. Existing per-namespace markers remain
a compatible fallback for tests/earlier deployments, not a second identity model.
Rollback before any new allocations is an atomic rename back to retained
evidence with extraction stopped. After allocations, never discard or lower
high-water values; preserve the registry even if image generation fails.

## Checksum and Safety Scope

PNG SHA256 values are reused from the previously verified census. They are
not freshly rehashed by this planner. Initial candidate metadata is freshly
hashed while decoding; revalidation reuses its retained hashes. All retained census paths are compared by size, nanosecond
mtime and file kind before and after planning. Annotations, Illustration
indexes, engravings, Book outputs, benchmark assets and the Companeros bird
are additionally rehashed. Per-file evidence identifies the method used.

This avoids repeated full traversals and image decoding. It does not prove
that metadata-preserving external changes are absent in files that were not
rehashed, or enumerate newly created files outside the census. Concurrent
catalog extraction is prevented with the existing shared lock; unrelated
external writers are not globally locked. Any detected baseline change
is reported. Revalidation separates pre-existing census differences from
changes during the current task, and compares against retained observed
confirmation metadata when available. Catalog/index drift blocks reuse and
requires fresh evidence, not an automatic census rebuild.

## Future Execution, Not Implemented

A separately approved executor would need active-dependency resolution,
destination collision checks, fresh content verification, a per-file journal,
an intentional production index transition and an interruption-safe rollback.
Same-filesystem individual renames can be atomic, but the entire operation
cannot be atomic. Rollback must verify source/destination checksums and refuse
overwrites. The manifest preserves original paths and hashes for recovery.
Neither record identity nor labels would change.