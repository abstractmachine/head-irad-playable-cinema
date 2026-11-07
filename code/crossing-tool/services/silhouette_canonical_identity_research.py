"""Isolated pilot and complete read-only canonical identity migration proposal.

Run with python -m services.silhouette_canonical_identity_research --project PATH.
Generated text/SQLite reports are regenerable research artifacts, not state I/O.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sqlite3
import stat
import sys
import time
from pathlib import Path

from services.silhouette_canonical_identity import (
    InstanceRegistry, POLICY, RESEARCH, label_slug, prepare_registration,
    proposed_artifact_directory, research_path,
)


DOMAINS = (
    "data/annotations", "data/silhouettes", "archive/silhouettes", "data/engravings",
    "data/indexes", "data/metadata", "data/palettes", "data/shotlists", "data/vocabulary",
    "outputs/books", "outputs/tests/engraving-benchmark",
    "outputs/tests/silhouette-identity-migration/archival-execution",
    "outputs/tests/silhouette-archive/v3-preparation-final",
)


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def output_database(path):
    database = sqlite3.connect(path, uri=True)
    database.execute("PRAGMA synchronous=FULL")
    database.execute("PRAGMA cache_size=-32768")
    return database


def safety_snapshot(project, destination):
    """Fresh complete lstat census, selected fresh hashes, no image decoding."""
    if destination.exists():
        raise FileExistsError(destination)
    start = time.monotonic()
    database = output_database(destination)
    database.execute("CREATE TABLE files(path TEXT PRIMARY KEY,kind TEXT,size INTEGER,mtime_ns INTEGER,mode INTEGER,inode INTEGER,target TEXT,sha256 TEXT)")
    pending = []
    counts = {}
    for domain in DOMAINS:
        root = project / domain
        count = 0
        stack = [root]
        while stack:
            path = stack.pop()
            relative = path.relative_to(project).as_posix()
            try:
                info = path.lstat()
            except FileNotFoundError:
                pending.append((relative, "missing", None, None, None, None, None, None))
                continue
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "symlink" if stat.S_ISLNK(info.st_mode) else "other"
            checksum = None
            if kind == "file" and (
                path.name == ".legacy-ordinal-reservations.sqlite3"
                or relative.startswith("data/indexes/illustration/")
                or relative.startswith("data/engravings/catalog/movie/Companeros (1970) {tmdb-61044}/bird/object_0002/")
                or relative.startswith("outputs/tests/silhouette-identity-migration/archival-execution/")
            ):
                checksum = digest(path)
            pending.append((relative, kind, info.st_size, info.st_mtime_ns, info.st_mode,
                            info.st_ino, os.readlink(path) if kind == "symlink" else None, checksum))
            count += kind == "file"
            if kind == "directory":
                with os.scandir(path) as entries:
                    stack.extend(Path(entry.path) for entry in entries)
            if len(pending) >= 4000:
                database.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", pending)
                database.commit()
                pending.clear()
        counts[domain] = count
        print("Safety", destination.name, domain, count, "files", flush=True)
    database.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", pending)
    database.commit()
    hashed = database.execute("SELECT count(*) FROM files WHERE sha256 IS NOT NULL").fetchone()[0]
    database.close()
    return {"seconds": time.monotonic()-start, "file_counts": counts, "fresh_sha256_files": hashed}


def compare_safety(before, after):
    with readonly(before) as database:
        database.execute("ATTACH DATABASE ? AS after", (after.resolve().as_uri()+"?mode=ro",))
        differences = database.execute(
            "SELECT count(*) FROM (SELECT * FROM files EXCEPT SELECT * FROM after.files)"
        ).fetchone()[0]
        additions = database.execute(
            "SELECT count(*) FROM (SELECT * FROM after.files EXCEPT SELECT * FROM files)"
        ).fetchone()[0]
    return {"changed_or_removed": differences, "changed_or_added": additions,
            "unchanged": differences == additions == 0,
            "method": "Complete fresh before/after lstat inventory (kind,size,mtime_ns,mode,inode,symlink target); selected files fresh SHA256 both times. Other content hashes not recomputed."}


def create_plan(path):
    database = output_database(path)
    database.executescript("""
    CREATE TABLE IF NOT EXISTS records(
      registration_key TEXT PRIMARY KEY,shot_id TEXT,media_type TEXT,media_id TEXT,
      legacy_identity TEXT,legacy_json TEXT UNIQUE,legacy_png TEXT,frame INTEGER,ordinal INTEGER,
      field TEXT,label TEXT,instance_id TEXT UNIQUE,destination TEXT UNIQUE,status TEXT DEFAULT 'ready');
    CREATE TABLE IF NOT EXISTS problems(path TEXT,category TEXT,detail TEXT);
    CREATE TABLE IF NOT EXISTS labels(label TEXT PRIMARY KEY,base_slug TEXT,slug TEXT UNIQUE,collision INTEGER);
    CREATE TABLE IF NOT EXISTS excluded(path TEXT PRIMARY KEY,media_type TEXT,state TEXT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
    """)
    return database


def stage_active(project, database):
    from services.illustration_index import index_path, load_index, _where, _default_assignment_state
    from services.silhouette_catalog import assignment_state_for_record
    start = time.monotonic()
    counts = {}
    keys = ("registration_key", "shot_id", "media_type", "media_id", "legacy_identity", "legacy_json", "legacy_png", "frame", "ordinal", "field", "label")
    for media in ("movie", "gameplay"):
        status = load_index(project, "silhouettes", media)
        if status.get("status") != "ready":
            raise ValueError("Ready index required: " + media)
        where, parameters = _where(assignment_state=_default_assignment_state("silhouettes", None))
        count = 0
        with readonly(index_path(project, "silhouettes", media)) as index:
            for payload, in index.execute(f"SELECT payload FROM records {where}", parameters):
                indexed = json.loads(payload)
                relative = str(indexed.get("path", ""))
                path = Path(relative)
                if not path.is_absolute():
                    path = project / path
                try:
                    if not path.resolve().is_relative_to(project / "data/silhouettes/catalog") or path.is_symlink():
                        raise ValueError("Unsafe/noncatalog record path")
                    record = json.loads(path.read_text())
                    record["path"] = path
                    if assignment_state_for_record(record) != "active":
                        raise ValueError("Index/catalog lifecycle disagreement")
                    for key in ("media_type", "media_id", "shot_id", "field", "label", "frame", "filename_stem"):
                        if record.get(key) != indexed.get(key):
                            raise ValueError("Index/catalog disagreement: " + key)
                    binding = prepare_registration(record, project)
                    if binding["media_type"] != media:
                        raise ValueError("Index/media partition mismatch")
                    png = project / binding["legacy_png"]
                    if png.is_symlink() or not png.is_file():
                        raise ValueError("Missing or unsafe PNG")
                    database.execute("INSERT INTO records("+",".join(keys)+") VALUES("+",".join("?" for _ in keys)+")", tuple(binding[key] for key in keys))
                except (ValueError, OSError, sqlite3.IntegrityError, TypeError) as error:
                    database.execute("INSERT INTO problems VALUES(?,?,?)", (relative, "input", str(error)))
                count += 1
                if count % 5000 == 0:
                    database.commit()
                if count % 100000 == 0:
                    print("Staged", media, count, flush=True)
        counts[media] = count
        database.commit()
    database.execute("CREATE INDEX IF NOT EXISTS initial_order ON records(shot_id,frame,ordinal,field,label,legacy_identity,legacy_json)")
    database.execute("CREATE INDEX IF NOT EXISTS classification ON records(field,label,shot_id)")
    database.commit()
    return {"seconds": time.monotonic()-start, "indexed_active": counts}


def reconcile_catalog_inventory(project, database, snapshot):
    """Account for nonindexed JSON without decoding images or archived metadata."""
    from services.silhouette_catalog import assignment_state_for_record
    database.execute("ATTACH DATABASE ? AS safety", (snapshot.resolve().as_uri()+"?mode=ro",))
    orphan_rows = database.execute(
        "SELECT path FROM safety.files WHERE kind='file' AND path GLOB 'data/silhouettes/catalog/*/*/*/object_*.json' "
        "AND path NOT IN (SELECT legacy_json FROM records) AND path NOT IN (SELECT path FROM problems)"
    ).fetchall()
    for path, in orphan_rows:
        record = json.loads((project/path).read_text())
        state = assignment_state_for_record(record)
        database.execute("INSERT INTO excluded VALUES(?,?,?)", (path, record.get("media_type"), state))
        if state == "active":
            database.execute("INSERT INTO problems VALUES(?,?,?)", (path, "accounting", "Active catalog record absent from ready index"))
    archive_records = database.execute("SELECT count(*) FROM safety.files WHERE kind='file' AND path GLOB 'archive/silhouettes/catalog/*/*/*/object_*.json'").fetchone()[0]
    database.commit()
    database.execute("DETACH DATABASE safety")
    return {"unindexed_catalog": len(orphan_rows), "archive_json_files_observed": archive_records}


def plan_slugs(database):
    labels = [row[0] for row in database.execute("SELECT DISTINCT label FROM records ORDER BY label")]
    groups = {}
    for label in labels:
        groups.setdefault(label_slug(label), []).append(label)
    collisions = []
    for base, members in groups.items():
        if len(members) > 1:
            collisions.append({"base_slug": base, "labels": members})
        for label in members:
            slug = base
            if len(members) > 1:
                slug += "--" + hashlib.sha256(label.encode()).hexdigest()
            try:
                database.execute("INSERT INTO labels VALUES(?,?,?,?)", (label, base, slug, int(len(members)>1)))
            except sqlite3.IntegrityError as error:
                database.execute("INSERT INTO problems VALUES(?,?,?)", (label, "slug", str(error)))
    database.commit()
    return collisions


def ordered_bindings(database, shots=None):
    database.row_factory = sqlite3.Row
    if shots:
        query = "SELECT * FROM records WHERE shot_id IN ("+",".join("?" for _ in shots)+")"
        parameters = shots
    else:
        query, parameters = "SELECT * FROM records", ()
    query += " ORDER BY shot_id,frame,ordinal,field,label,legacy_identity,legacy_json"
    cursor = database.execute(query, parameters)
    while batch := cursor.fetchmany(2000):
        yield [dict(row) for row in batch]


def allocate_plan(project, database, registry_path):
    start = time.monotonic()
    registry = InstanceRegistry(project, registry_path)
    allocated = 0
    processed = 0
    try:
        for batch in ordered_bindings(database):
            allocations = registry.register(batch, ordered=True)
            for key, instance, new in allocations:
                allocated += new
                database.execute("UPDATE records SET instance_id=? WHERE registration_key=?", (instance, key))
            database.commit()
            processed += len(batch)
            if processed % 100000 == 0:
                print("Allocated", processed, "new", allocated, flush=True)
        transactions = registry.transactions
        count = registry.database.execute("SELECT count(*) FROM instances").fetchone()[0]
        shots = registry.database.execute("SELECT count(*) FROM shots").fetchone()[0]
        maximum = registry.database.execute("SELECT max(high_water) FROM shots").fetchone()[0]
    finally:
        registry.close()
    return {"seconds": time.monotonic()-start, "new_assignments": allocated, "processed": processed,
            "registered": count, "shots": shots, "max_records_per_shot": maximum, "transactions": transactions}


def build_destinations(database):
    start = time.monotonic()
    cursor = database.execute("SELECT registration_key,media_type,field,labels.slug,instance_id FROM records JOIN labels USING(label)")
    updates = []
    for key, media, field, slug, instance in cursor:
        try:
            path = proposed_artifact_directory("silhouettes", media, field, slug, instance)
            updates.append((path, key))
        except ValueError as error:
            database.execute("INSERT INTO problems VALUES(?,?,?)", (key, "destination", str(error)))
        if len(updates) == 2000:
            database.executemany("UPDATE records SET destination=? WHERE registration_key=?", updates)
            database.commit()
            updates.clear()
    database.executemany("UPDATE records SET destination=? WHERE registration_key=?", updates)
    database.execute("CREATE VIEW IF NOT EXISTS file_mappings AS SELECT registration_key,instance_id,legacy_json AS source,destination||'/silhouette.json' AS destination FROM records UNION ALL SELECT registration_key,instance_id,legacy_png,destination||'/silhouette.png' FROM records")
    database.commit()
    return time.monotonic()-start


def real_pilot(project, database, output):
    start = time.monotonic()
    output.mkdir(exist_ok=True)
    shots = set()
    companions = database.execute("SELECT shot_id FROM records WHERE legacy_json LIKE '%/Companeros (1970) {tmdb-61044}/bird/object_0002.json'").fetchone()
    if companions is None:
        raise ValueError("Companeros bird absent")
    shots.add(companions[0])
    for query in (
        "SELECT shot_id FROM records GROUP BY shot_id ORDER BY count(DISTINCT label) DESC LIMIT 1",
        "SELECT shot_id FROM records GROUP BY shot_id,field,label ORDER BY count(*) DESC LIMIT 1",
        "SELECT shot_id FROM records GROUP BY shot_id ORDER BY count(DISTINCT field) DESC LIMIT 1",
        "SELECT shot_id FROM records WHERE media_type='gameplay' ORDER BY legacy_json LIMIT 1",
    ):
        shots.add(database.execute(query).fetchone()[0])
    for field in ("objects", "animals", "wearing", "action", "setting"):
        row = database.execute("SELECT shot_id FROM records WHERE field=? ORDER BY legacy_json LIMIT 1", (field,)).fetchone()
        if row:
            shots.add(row[0])
    bindings = [record for batch in ordered_bindings(database, sorted(shots)) for record in batch]
    registry = InstanceRegistry(project, output/"registry.sqlite3")
    try:
        allocations = registry.register(bindings)
        assert not any(new for _, _, new in registry.register(bindings))
        original = bindings[0]
        corrected = {**original, "label": "hawk", "field": "animals", "legacy_json": "simulated/new/location/silhouette.json"}
        assert registry.register([corrected])[0][1] == registry.lookup(original["registration_key"])[0]
        registry.retire(original["registration_key"])
        assert not registry.register([original])[0][2]
        inserted = {**original, "registration_key": hashlib.sha256(b"pilot-new-record").hexdigest(),
                    "legacy_json": "isolated-pilot/new-record.json", "legacy_png": "isolated-pilot/new-record.png", "ordinal": 0}
        inserted_id = registry.register([inserted])[0][1]
        interrupted = {**inserted, "registration_key": hashlib.sha256(b"pilot-interrupted-record").hexdigest(),
                       "legacy_json": "isolated-pilot/interrupted.json"}
        def interrupt(count):
            raise InterruptedError("Pilot transaction interruption")
        try:
            registry.register([interrupted], interrupt=interrupt)
        except InterruptedError:
            pass
        assert registry.lookup(interrupted["registration_key"]) is None
        resumed_id = registry.register([interrupted])[0][1]
        report = {"seconds": time.monotonic()-start, "real_records": len(bindings), "shots": sorted(shots),
                  "fields": sorted({record["field"] for record in bindings}), "allocations": allocations,
                  "classification_and_path_change_stable": True, "retirement_reserved": True,
                  "inserted_instance": inserted_id, "resumed_instance": resumed_id, "interruption_rolled_back": True}
    finally:
        registry.close()
    (output/"pilot.json").write_text(json.dumps(report, indent=2))
    return {key: value for key, value in report.items() if key != "allocations"}


def analysis(database):
    database.row_factory = None
    count = lambda query: database.execute(query).fetchone()[0]
    return {
        "active_records_staged": count("SELECT count(*) FROM records"),
        "identity_collisions": count("SELECT count(*) FROM (SELECT instance_id FROM records GROUP BY instance_id HAVING count(*)>1)"),
        "destination_directory_collisions": count("SELECT count(*) FROM (SELECT destination FROM records WHERE destination IS NOT NULL GROUP BY destination HAVING count(*)>1)"),
        "destination_file_collisions": count("SELECT count(*) FROM (SELECT destination FROM file_mappings WHERE destination IS NOT NULL GROUP BY destination HAVING count(*)>1)"),
        "unresolved_destinations": count("SELECT count(*) FROM records WHERE destination IS NULL"),
        "problems": count("SELECT count(*) FROM problems"),
        "excluded": list(database.execute("SELECT state,count(*) FROM excluded GROUP BY state")),
        "same_shot_repeated_legacy_ordinals": count("SELECT count(*) FROM (SELECT shot_id,ordinal FROM records GROUP BY shot_id,ordinal HAVING count(*)>1)"),
        "same_shot_multiple_labels": count("SELECT count(*) FROM (SELECT shot_id FROM records GROUP BY shot_id HAVING count(DISTINCT label)>1)"),
        "same_shot_multiple_fields": count("SELECT count(*) FROM (SELECT shot_id FROM records GROUP BY shot_id HAVING count(DISTINCT field)>1)"),
        "same_shot_repeated_field_label": count("SELECT count(*) FROM (SELECT shot_id,field,label FROM records GROUP BY shot_id,field,label HAVING count(*)>1)"),
        "same_shot_same_frame_multiple_records": count("SELECT count(*) FROM (SELECT shot_id,frame FROM records GROUP BY shot_id,frame HAVING count(*)>1)"),
    }


def write_report(output, summary):
    allocation = summary["allocation"]
    result = summary["analysis"]
    bird = summary["companeros"]
    collisions = "\n".join(f"- `{group['base_slug']}`: {', '.join(repr(label) for label in group['labels'])}" for group in summary["slug_collisions"])
    unresolved = "\n".join(f"- `{record['legacy_json']}`: {record['reason']}" for record in summary.get("unresolved_active_records", []))
    report = f"""# Canonical Silhouette Identity: Research Proposal

## A. Current Identity Architecture

Current exact catalog reference is `(media_type, filename_stem, label, object_id)`
from `services.silhouette_catalog.catalog_object_reference`, backed by the original
JSON path. Extraction identity `(media_type, media_id, shot_id, field, search_label)`
groups requests, not individual records. Object ordinals are label-directory-wide,
not shot-wide. No existing durable classification-independent record token was
found. Models, timestamps, PNG content and descriptive labels are not substitutes.

`data.media_id.build_shot_id` / `parse_shot_id` own movie and gameplay shot grammar;
this experiment validates and reuses it. IDs encode inclusive frame boundaries:
they are stable for unchanged shot boundaries, not promises across shotlist edits.
Source shot/media is immutable after registration; corrections to source identity
require an explicit adjudicated operation, not automatic reallocation.

Real active evidence: {result['same_shot_repeated_legacy_ordinals']} repeated
shot/legacy-ordinal groups; {result['same_shot_multiple_labels']} multi-label shots;
{result['same_shot_multiple_fields']} multi-field shots;
{result['same_shot_repeated_field_label']} repeated shot/field/label groups;
{result['same_shot_same_frame_multiple_records']} repeated shot/frame groups.
Thus neither label, frame, ordinal nor extraction request uniquely identifies a
record. This allocator represents registered records, not physical scene objects.

Ordinary extraction caches a completed label/shot unless forced, saves each new
mask with a fresh legacy ordinal, and writes active assignment atomically.
Corrections/rechecks group exact source jobs, extract current atomic annotation
values, stage replacements inactive, then activate replacements and supersede
historical predecessors. Reruns do not imply physical-object identity or merging.
Classification, validation, lifecycle, lineage and artifacts remain separate.

## B. Proposed Canonical Registry

`registry-proposal.sqlite3`: `instances` binds registration_key to instance_id,
shot_id/instance_number, media type/ID, immutable original legacy identity and
JSON/PNG paths, registration_state, created_at. `shots` retains high_water.
Unique constraints cover registration_key, instance_id, original JSON and
(shot_id, instance_number). Foreign keys and immutable-binding/deletion/high-water
triggers enforce reservation persistence. BEGIN IMMEDIATE + FULL synchronous
transactions serialize concurrent allocation; failures roll back whole batches.

Initial registration keys are deterministic SHA256 tokens of the exact original
catalog reference and original JSON path. This is a one-time binding, not a
path-derived future identity. Later callers MUST retain registration_key or
resolve instance_id through the registry; they must not recompute a token after a
move or relabel. The binding remains authoritative if paths/classification change.
Losing the registry/binding is not repairable by a fresh sort of the current files.
Retirement keeps identities reserved; reintroduction returns the same identity
without silently reactivating lifecycle or registration state.

## C. Allocation Policy

Initial total order: shot ID, integer source frame, integer legacy ordinal,
exact field, exact label, canonical serialized legacy reference, original path.
Filesystem traversal never determines numbering. This rule only orders NEW
bindings; registered records never renumber. Incremental records append above
shot high_water even if they would sort earlier. Concurrent different new records
are serialized in transaction arrival order; deterministic initial corpus ordering
does not assert timing-independent ordering for concurrent future insertions.
Minimum suffix width is four digits, expanding naturally beyond 9999. Observed
maximum: {allocation['max_records_per_shot']}; no arbitrary ceiling is imposed.

## D. Pilot

{summary['pilot']['real_records']} real records from {len(summary['pilot']['shots'])}
complete source-shot groups; fields: {', '.join(summary['pilot']['fields'])}.
Includes Companeros, multiple labels, repeated field/label instances, multiple
fields and RDR2. Full allocations/shot IDs are in pilot/pilot.json and its registry.
Pilot classification/path corrections, append insertion, retirement/reintroduction
and interrupted transaction/resume passed without touching original artifacts.
Concurrency, registry restart, missing components and duplicate protection use
isolated synthetic tests. Pilot runtime: {summary['pilot']['seconds']:.3f} seconds.

Companeros bird original: `{bird['legacy_json']}`.
Proposed identity: `{bird['instance_id']}` (not inferred from object_0002).
The existing frame engraving at
`data/engravings/catalog/movie/Companeros (1970) {{tmdb-61044}}/bird/object_0002/frame/engraving.json`
retains its original source.silhouette_json and local Qwen provenance. A future
consumer would resolve that original JSON binding to `{bird['instance_id']}`;
this experiment changes neither engraving metadata nor artifacts.

## E. Full-Corpus Results

Indexed active counts: {json.dumps(summary['stage']['indexed_active'])}.
Staged/registered: {result['active_records_staged']} / {allocation['registered']}.
Unique shots: {allocation['shots']}. Identity collisions: {result['identity_collisions']}.
Destination directory/file collisions: {result['destination_directory_collisions']}
/ {result['destination_file_collisions']}. Unresolved destinations:
{result['unresolved_destinations']}. Input/path problems: {result['problems']}.
Unindexed exclusions: {result['excluded']}. Archive JSONs counted by filesystem
metadata only: {summary['inventory']['archive_json_files_observed']}; no allocation
or content rewrite for them. Missing component and identifier statistics are
explicit in summary.json; all rejected inputs remain in problems, never discarded.

**Not a complete allocation:** all {sum(summary['stage']['indexed_active'].values())}
active inputs are accounted for, but {len(summary.get('unresolved_active_records', []))}
records are withheld because no authoritative field is present. Do not interpret
zero missing fields among staged VALID rows as zero missing fields in the corpus.
Corpus-wide missing-field count: {summary.get('corpus_missing_fields', 'not yet expanded')}.
The `unresolved_records` table preserves their original JSON/PNG, source media,
shot, legacy reference and metadata; `complete_file_mappings` includes their
NULL destinations. No field is inferred from a label and no IDs are invented.
Exact blocked records:
{unresolved or 'See problems table; expanded reporting not yet finalized.'}

## F. Slugging

NFKC Unicode normalization, casefold, Unicode alphanumeric characters retained,
all separators/punctuation collapsed to hyphens, edges trimmed. No semantic
normalization; original exact labels remain stored. Collision groups:
{collisions or 'None'}

Proposed collision policy is `<base>--<full SHA256 of exact original label>` for
EVERY member of a collision group. Full label-to-slug mapping is reversible via
the labels table; hashes are deterministic, not random. This suffix refinement
requires approval before finalizing the filesystem convention. No labels merge.
The registry remains unchanged if approved classification/slug policy changes.
Unicode runtime version: {summary['unicode_version']}.

## G. Proposed Filesystem

`data/silhouettes/<media>/<field>/<label-slug>/<instance-id>/silhouette.{{png,json}}`.
No catalog, title directory, object prefix, backend/type/mode in instance IDs.
Central helper: `proposed_artifact_directory`; current consumers are unchanged.
Companeros destination directory: `{bird['destination']}`.
`migration-plan.sqlite3.records` maps every binding; `file_mappings` exposes old
JSON/PNG to exact proposed filenames. Two mappings per valid record. SQLite unique
constraints plus full group collision queries check destination uniqueness.

## H. Historical Compatibility

Original legacy reference, JSON/PNG paths, source media and shot survive in the
registry even after future classification or relocation. Historical archive IDs,
assignment/recheck lineage and report references are unchanged. Existing explicit
archive resolver and manifest mapping remain authoritative for archived records.
The legacy ordinal reservation registry prevents old directory ordinals recycling;
the new registry assigns shot-wide instances. They are separate and neither is
replaced or merged. No canonical IDs are assigned to archived/inactive records.

## I. Downstream Dependencies

| Consumer | Required future migration work, not implemented here |
| --- | --- |
| Illustration indexes | Index instance_id/registration_key, update source path scanning and preserve active-only semantics; rebuild only during authorized migration. |
| SilhouetteSource/EngravingSource | Replace sibling PNG and title-directory/object-stem assumptions with canonical artifact/binding resolution. |
| Exact-record navigation | Extend existing identity resolver to instance_id; retain registry-backed legacy aliases and authoritative engraving source references; no label-only fallback. |
| Annotation QA/curation/recheck | Keep source-shot/field/atomic label semantics separate; record stable instances and preserve predecessor/replacement references through registry aliases. |
| Engraving generation/paths/local publisher | Derive source instance from registry, not JSON stem; retain mode/backend as artifact/provenance axes, implement multi-mode layout explicitly before migration. |
| Engraving lifecycle/index | Replace catalog hierarchy parsing, legacy object IDs and source-path validation with registry-aware references; preserve run hashes and backend provenance. |
| Books/generators | Self-contained copied book images stay untouched; audit absolute layer references and drag/drop/generator candidate paths; do not rewrite copies indiscriminately. |
| Historical reference resolution | Resolve migrated ACTIVE legacy references through explicit registry bindings, leave archived paths/manifests and historical checksums intact. |
| MCP discovery/reference tools | Update canonical services' catalog-root authorization and candidate IDs; MCP stays a thin wrapper, not a second resolver. |
| CLI commands | Expose reviewed registration/migration operations in canonical services, update path/identity argument handling; no physical migration CLI exists in this experiment. |
| Benchmarks/manifests | Preserve original source references and immutable evidence, add canonical bindings via explicit adapters; no rewriting historical manifests. |

Inspection anchors: silhouette_catalog/extraction/recheck/curation,
silhouette_legacy_allocation, illustration_index/source/browser,
engraving_paths/lifecycle, silhouette_discovery/reference,
silhouette_archive_reference, data/book, generators and MCP service wrappers.
Engravings with several modes cannot all map to one engraving.png unchanged;
their future artifact layout remains a separate design/authorization task.

## J. Performance And Reproducibility

JSON/index staging: {summary['stage']['seconds']:.3f}s; full allocation:
{allocation['seconds']:.3f}s; second registry replay: {summary['rerun']['seconds']:.3f}s,
new assignments {summary['rerun']['new_assignments']}. Allocation/replay transactions:
{allocation['transactions']} / {summary['rerun']['transactions']} (2000-record batches).
Destination construction: {summary['destination_seconds']:.3f}s; collision analysis:
{summary['collision_analysis_seconds']:.3f}s. Peak process RSS: {summary['peak_rss_kib']} KiB.
Database sizes bytes: {summary['database_sizes']}. Total including both complete
safety inventories: {summary['runtime_seconds']:.3f}s. Rows stream with bounded
batches and disk-backed ordering. PNGs were neither read into memory nor decoded.
Independent initial-registry tests verify deterministic allocation; full registry
replay verifies no new assignments or changed existing IDs, not byte-identical
SQLite files or created_at timestamps across independent experiments.

## K. Safety

{summary['safety']['method']}
Unchanged: {summary['safety']['unchanged']}; before/after difference counts:
{summary['safety']['changed_or_removed']} / {summary['safety']['changed_or_added']}.
Freshly hashed selected files: {summary['safety_before']['fresh_sha256_files']}.
Per-domain file counts and timings are in summary.json; complete inventories are
safety-before.sqlite3 / safety-after.sqlite3. Selected hashes include Illustration
indexes, legacy reservations, Companeros engraving/local run files, archival
journal and execution evidence. Existing archival verification evidence supplies
prior content hashes for the archive; this task does NOT claim fresh SHA256 of
every silhouette/archive PNG. Directory/file metadata was freshly verified.
An audit guard restricted Python writes and writable SQLite connections to the
research outputs. Production inputs used read-only SQLite. No production data,
index, annotation, archive, book, engraving or generation mutations were performed.

## L. Recommendation

Do not authorize a physical migration yet. Remaining blockers:
{chr(10).join('- '+blocker for blocker in summary['blockers'])}

Review the complete registry/mappings and suffix policy, then design and test
consumer adapters, source-shot correction policy and downstream multi-artifact
layout before a separately authorized migration. This task stops at the proposal.
"""
    (output/"report.md").write_text(report)


def install_write_guard(output):
    def audit(event, args):
        if event == "open":
            name, mode, flags = args
            if isinstance(name, (str, bytes, os.PathLike)) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC):
                if not Path(os.fsdecode(name)).resolve().is_relative_to(output):
                    raise PermissionError("Research write outside authorized output: " + str(name))
        elif event == "sqlite3.connect":
            name = str(args[0])
            if not name.endswith("?mode=ro") and not Path(name).resolve().is_relative_to(output):
                raise PermissionError("Production SQLite must be read-only")
        elif event in ("os.rename", "os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"):
            paths = args[:2] if event == "os.rename" else args[:1]
            for path in paths:
                if not Path(path).resolve().is_relative_to(output):
                    raise PermissionError("Mutation outside research outputs")
    sys.addaudithook(audit)


def finalize_unresolved_report(project):
    """Preserve full rejected-record facts and accounting without new allocation."""
    from services.silhouette_catalog import catalog_object_reference
    project = Path(project).resolve()
    output = project / RESEARCH
    research_path(project, output)
    summary = json.loads((output/"summary.json").read_text())
    records = []
    with output_database(output/"migration-plan.sqlite3") as database:
        database.execute("CREATE TABLE IF NOT EXISTS unresolved_records(legacy_json TEXT PRIMARY KEY,legacy_png TEXT,media_type TEXT,media_id TEXT,shot_id TEXT,field TEXT,label TEXT,legacy_identity TEXT,reason TEXT,metadata TEXT)")
        for path, reason in database.execute("SELECT path,detail FROM problems WHERE category='input' ORDER BY path"):
            source = project / path
            if not source.resolve().is_relative_to(project/"data/silhouettes/catalog"):
                raise ValueError("Unsafe problem reference")
            metadata = json.loads(source.read_text())
            record = {"legacy_json": path, "legacy_png": Path(path).with_suffix('.png').as_posix(),
                      "reason": reason, **{key: metadata.get(key) for key in ('media_type','media_id','shot_id','field','label')}}
            legacy = json.dumps(catalog_object_reference(metadata, source), sort_keys=True, ensure_ascii=False)
            database.execute("INSERT OR REPLACE INTO unresolved_records VALUES(?,?,?,?,?,?,?,?,?,?)", (
                record['legacy_json'], record['legacy_png'], record['media_type'], record['media_id'],
                record['shot_id'], record['field'], record['label'], legacy, reason, json.dumps(metadata, ensure_ascii=False),
            ))
            records.append(record)
        database.execute("CREATE VIEW IF NOT EXISTS complete_file_mappings AS SELECT * FROM file_mappings UNION ALL SELECT NULL,NULL,legacy_json,NULL FROM unresolved_records UNION ALL SELECT NULL,NULL,legacy_png,NULL FROM unresolved_records")
        summary["complete_file_mappings"] = database.execute("SELECT count(*) FROM complete_file_mappings").fetchone()[0]
        summary["unresolved_file_mappings"] = database.execute("SELECT count(*) FROM complete_file_mappings WHERE destination IS NULL").fetchone()[0]
        summary["active_records_accounted"] = database.execute("SELECT (SELECT count(*) FROM records)+(SELECT count(*) FROM unresolved_records)").fetchone()[0]
    summary["unresolved_active_records"] = records
    summary["corpus_missing_fields"] = summary["analysis"]["missing_field"] + sum(not record['field'] for record in records)
    summary["withheld_active_registrations"] = len(records)
    summary["database_sizes"]["migration-plan.sqlite3"] = (output/"migration-plan.sqlite3").stat().st_size
    summary["blockers"] = ["Six active records have no authoritative field; registration and destination planning withheld without inventing classification",
                           "Collision slug suffix policy requires approval",
                           "Consumers require explicit migration implementation and authorization"]
    summary["allocator_validation"] = "Focused and broader test results are recorded separately after final source checks."
    (output/"summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    write_report(output, summary)
    return summary


def run(project):
    project = Path(project).resolve()
    output = project / RESEARCH
    output.mkdir(parents=True, exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = str(output)
    original_connect = sqlite3.connect
    def guarded_connect(path, *args, **kwargs):
        name = str(path)
        if not name.startswith("file:") and not Path(name).resolve().is_relative_to(output):
            name = Path(name).resolve().as_uri()+"?mode=ro"
            kwargs["uri"] = True
        return original_connect(name, *args, **kwargs)
    sqlite3.connect = guarded_connect
    install_write_guard(output)
    start = time.monotonic()
    if (output/"summary.json").exists():
        raise FileExistsError("Completed experiment exists; preserve it and review before another run")
    summary = {"policy": POLICY, "project": str(project)}
    summary["safety_before"] = safety_snapshot(project, output/"safety-before.sqlite3")
    database = create_plan(output/"migration-plan.sqlite3")
    try:
        summary["stage"] = stage_active(project, database)
        summary["inventory"] = reconcile_catalog_inventory(project, database, output/"safety-before.sqlite3")
        summary["slug_collisions"] = plan_slugs(database)
        summary["pilot"] = real_pilot(project, database, output/"pilot")
        summary["allocation"] = allocate_plan(project, database, output/"registry-proposal.sqlite3")
        summary["rerun"] = allocate_plan(project, database, output/"registry-proposal.sqlite3")
        summary["destination_seconds"] = build_destinations(database)
        analysis_start = time.monotonic()
        summary["analysis"] = analysis(database)
        summary["collision_analysis_seconds"] = time.monotonic()-analysis_start
        summary["companeros"] = dict(zip(("registration_key", "instance_id", "legacy_json", "legacy_png", "destination"),
            database.execute("SELECT registration_key,instance_id,legacy_json,legacy_png,destination FROM records WHERE legacy_json LIKE '%/Companeros (1970) {tmdb-61044}/bird/object_0002.json'").fetchone()))
        for key in ("media_id", "shot_id", "field", "label"):
            summary["analysis"]["missing_"+key] = database.execute(f"SELECT count(*) FROM records WHERE {key} IS NULL OR {key}='' ").fetchone()[0]
        summary["analysis"]["invalid_instance_identifiers"] = 0
        from services.silhouette_canonical_identity import instance_identifier
        for shot, instance in database.execute("SELECT shot_id,instance_id FROM records"):
            try:
                if instance_identifier(shot, int(instance.rsplit('#',1)[1])) != instance:
                    raise ValueError()
            except (ValueError, TypeError):
                summary["analysis"]["invalid_instance_identifiers"] += 1
        database.execute("INSERT OR REPLACE INTO meta VALUES('policy',?)", (POLICY,))
        database.commit()
    finally:
        database.close()
    summary["safety_after"] = safety_snapshot(project, output/"safety-after.sqlite3")
    summary["safety"] = compare_safety(output/"safety-before.sqlite3", output/"safety-after.sqlite3")
    summary["database_sizes"] = {name: (output/name).stat().st_size for name in ("registry-proposal.sqlite3", "migration-plan.sqlite3")}
    summary["runtime_seconds"] = time.monotonic()-start
    summary["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    import unicodedata
    summary["unicode_version"] = unicodedata.unidata_version
    summary["ready_for_physical_migration"] = False
    summary["blockers"] = ["Collision slug suffix policy requires approval", "Consumers require explicit migration implementation and authorization"]
    if summary["analysis"]["problems"] or summary["analysis"]["unresolved_destinations"]:
        summary["blockers"].append("Recorded input/path problems require review")
    if not summary["safety"]["unchanged"]:
        summary["blockers"].append("Protected production census changed")
    if summary["stage"]["indexed_active"] != {"movie": 662804, "gameplay": 5246}:
        summary["blockers"].append("Active population differs from approved expected counts")
    (output/"summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    write_report(output, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--finalize-report", action="store_true", help="Expand existing unresolved-input evidence; no allocation")
    arguments = parser.parse_args()
    if arguments.finalize_report:
        finalize_unresolved_report(arguments.project)
    else:
        run(arguments.project)