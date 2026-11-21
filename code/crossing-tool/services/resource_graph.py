"""Fixture-only installation evidence for the frozen media-first resource graph."""

from contextlib import closing
from functools import wraps
import json
import os
from pathlib import Path
import sqlite3

from data import resource_locations as locations
from data.resource_storage import digest
from data.resource_storage import ResourceStore, create_storage_directory, sync_directory, relative_path
from services import resource_migration as migration
from services import resource_rename as renames
from services.maintenance_preflight import check_quiescence
from tool import project_access as access


def _fixture_access(function):
    @wraps(function)
    def guarded(owner, *args, **kwargs):
        with access.fixture_graph_access(owner):
            return function(owner, *args, **kwargs)
    return guarded


def graph_storage_budget(plan, *, preparation_bytes, fragment=4096):
    """Stream the frozen mapping; do not inspect payloads or allocate an inventory."""
    if type(fragment) is not int or fragment < 4096 or type(preparation_bytes) is not int or preparation_bytes < 0:
        raise ValueError("Invalid storage accounting parameters")
    def rounded(size):
        return ((size + fragment - 1) // fragment) * fragment
    counts = {}
    existing = {}
    directories = 0
    directory_bytes = 0
    entries_bytes = 0
    path_bytes = 0
    move_evidence = 0
    transform_evidence = 0
    transformed_blocks = 0
    transformed_bytes = 0
    largest_metadata = 0
    registry_growth = 0
    pairs = 0
    previous = ()
    with closing(migration.readonly(Path(plan).resolve(strict=True))) as database:
        for disposition, count, size in database.execute("SELECT disposition,count(*),sum(size) FROM files GROUP BY disposition"):
            counts[disposition], existing[disposition] = count, size or 0
        for row in database.execute("SELECT * FROM files WHERE disposition IN ('Move','Transform') ORDER BY destination"):
            parts = tuple(row["destination"].split("/")[:-1])
            common = 0
            while common < min(len(parts), len(previous)) and parts[common] == previous[common]:
                common += 1
            for position in range(common, len(parts)):
                directories += 1
                directory_bytes += len("/".join(parts[:position + 1]).encode())
                entries_bytes += len(parts[position].encode()) + 32
            previous = parts
            entries_bytes += len(Path(row["destination"]).name.encode()) + 32
            path_bytes += len(row["source"].encode()) + len(row["destination"].encode())
            if row["disposition"] == "Move":
                move_evidence += len(renames._encode(dict(row)).encode())
        for row in database.execute("SELECT f.*,t.schema,t.patch,t.after_sha256,t.after_bytes FROM files f JOIN transformations t USING(source) WHERE f.disposition='Transform'"):
            transform_evidence += len(renames._encode(dict(row)).encode())
            size = row["after_bytes"]
            transformed_bytes += size
            transformed_blocks += rounded(size)
            largest_metadata = max(largest_metadata, size)
            if row["schema"] in ("silhouette-v1", "engraving-v2"):
                pairs += 1
                registry_growth += rounded(8192 + 4 * len(row["destination"].encode()))
        largest_index = database.execute("SELECT coalesce(max(size),0) FROM files WHERE disposition='Rebuild'").fetchone()[0]
    total_records = counts.get("Move", 0) + counts.get("Transform", 0)
    registry = rounded(preparation_bytes) + registry_growth + 4 * rounded(largest_metadata)
    old_indexes = existing.get("Rebuild", 0)
    components = {
        "renamed_payload_copies": 0,
        "metadata_before_image_copies": 0,
        "transformed_metadata_blocks": transformed_blocks,
        "execution_journals": 2 * rounded(move_evidence + transform_evidence + total_records * 1024 + path_bytes * 2) + 32 * 1024**2,
        "one_working_registry": registry,
        "replacement_indexes": 2 * rounded(old_indexes),
        "sqlite_temp_and_rollback": max(2 * rounded(largest_index), 64 * 1024**2),
        "directory_and_entry_blocks": directories * 2 * fragment + 2 * rounded(entries_bytes),
        "inode_and_control_metadata": rounded((directories + counts.get("Transform", 0) + 256) * 1024),
        "unsealed_retention_and_rollback": rounded(preparation_bytes) + 2 * rounded(old_indexes) + 4 * rounded(largest_metadata) + 64 * 1024**2,
        "safety_margin": 4 * 1024**3,
    }
    return {"counts": counts, "existing_payload_bytes": existing, "unique_destination_directories": directories,
            "unique_directory_path_bytes": directory_bytes, "destination_entry_bytes": entries_bytes,
            "path_bytes": path_bytes, "move_evidence_bytes": move_evidence, "transform_evidence_bytes": transform_evidence,
            "transformed_logical_bytes": transformed_bytes, "pairs": pairs, "fragment": fragment,
            "components": components, "peak_additional_bytes": sum(components.values()),
            "additional_inode_bound": directories + counts.get("Transform", 0) + 256,
            "production_approved": False,
            "assumptions": ["sequential owners", "one working registry", "one unsealed staging retention per output",
                            "replacement outputs bounded by twice frozen old sizes", "SQLite overlap bounded by reserved working-set term",
                            "registry mutations are row-scoped, not full-snapshot replacements",
                            "two directory blocks plus twice encoded entries", "Task 4 validates physical allocation assumptions"]}


def _exact(project, reference, checksum):
    relative_path(project, reference)
    path = project / reference
    if not os.path.lexists(path):
        return False
    path = locations.project_file(project, reference)
    if path.stat().st_nlink != 1 or digest(path) != checksum:
        raise ValueError("Changed content or unexpected recovery artifact: " + reference)
    return True


def _move(project, source, destination):
    with access._root(project) as (_path, root):
        with renames._parent(root, renames._parts(project, source)) as (source_parent, source_name):
            parts = destination.split("/")
            if any(part in ("", ".", "..") for part in parts) or Path(destination).is_absolute():
                raise ValueError("Unsafe recovery destination")
            with renames._parent(root, parts, create=True) as (destination_parent, destination_name):
                renames._rename(source_parent, source_name, destination_parent, destination_name)
                os.fsync(source_parent)
                os.fsync(destination_parent)


def _relocate(project, source, destination, checksum):
    relative_path(project, source)
    relative_path(project, destination)
    if not _exact(project, source, checksum):
        raise ValueError("Missing source before graph rename")
    if source.startswith(access.CONTROL + "/"):
        with access._root(project) as (_path, root):
            with renames._parent(root, source.split("/")) as (source_parent, source_name):
                with renames._parent(root, destination.split("/"), create=True) as (destination_parent, destination_name):
                    renames._rename(source_parent, source_name, destination_parent, destination_name)
                    os.fsync(source_parent)
                    os.fsync(destination_parent)
    else:
        _move(project, source, destination)


def _handoff(owner, plan, checksum, revision, clients_stopped, *, verify_resources=True):
    state = owner.require_fixture_renames()
    check_quiescence(owner, clients_stopped=clients_stopped)
    project = Path(state["project"])
    path, reference, fingerprint = renames._plan(project, plan, checksum, revision)
    journal = project / access.CONTROL / ("rename-" + state["operation"] + ".sqlite3")
    with closing(migration.readonly(journal)) as database, closing(migration.readonly(path)) as frozen:
        headers = database.execute("SELECT * FROM meta").fetchall()
        if len(headers) != 1 or headers[0]["direction"] != "forward":
            raise ValueError("Completed Task 2 forward handoff required")
        header = json.loads(headers[0]["payload"])
        metadata = {row[0]: json.loads(row[1]) for row in frozen.execute("SELECT * FROM meta")}
        if (header["plan_digest"] != checksum or header["plan_identity"] != revision
                or header["token"] != state["token"] or header["operation"] != state["operation"]
                or metadata.get("project") != str(project)
                or metadata.get("plan_revision", {}).get("stage", metadata.get("stage")) != revision):
            raise ValueError("Task 2 handoff plan/project binding mismatch")
        for actual, expected in zip(database.execute("SELECT * FROM moves ORDER BY source"), renames._rows(frozen), strict=True):
            if actual["state"] != "done" or json.loads(actual["frozen"]) != expected:
                raise ValueError("Task 2 file movement is incomplete")
            if verify_resources and not _exact(project, expected["destination"], expected["sha256"]):
                raise ValueError("Missing relocated resource")
        if frozen.execute("SELECT 1 FROM issues WHERE blocking<>0 LIMIT 1").fetchone():
            raise ValueError("Frozen plan has blocking issues")
        graph = project / access.CONTROL / ("graph-" + state["operation"] + ".sqlite3")
        if os.path.lexists(graph):
            with closing(migration.readonly(locations.project_file(project, graph))) as evidence:
                _integrity(evidence)
                rows = evidence.execute("SELECT payload FROM meta").fetchall()
                expected_header = {"project": str(project), "token": state["token"], "operation": state["operation"],
                                   "plan": reference, "sha256": checksum, "revision": revision, "stat": list(fingerprint)}
                if len(rows) != 1 or json.loads(rows[0][0]) != expected_header:
                    raise ValueError("Graph journal binding mismatch")
    return state, project, path, reference, fingerprint


def _commit(database, parent, interrupt, phase):
    renames._event(interrupt, phase + ":before-sync")
    import time
    database.execute("CREATE TABLE IF NOT EXISTS progress_clock(started REAL NOT NULL,updated REAL NOT NULL,phase TEXT NOT NULL)")
    now = time.time()
    if database.execute("SELECT 1 FROM progress_clock").fetchone() is None:
        database.execute("INSERT INTO progress_clock VALUES(?,?,?)", (now, now, phase))
    else:
        database.execute("UPDATE progress_clock SET updated=?,phase=?", (now, phase))
    database.commit()
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    renames._event(interrupt, phase + ":synced")


def _state(database, parent, number, value, interrupt):
    database.execute("UPDATE transforms SET state=? WHERE number=?", (value, number))
    _commit(database, parent, interrupt, "transform:" + value)


@_fixture_access
def transform_fixture_metadata(owner, plan, *, approved_digest, plan_identity, clients_stopped=False,
                               action="forward", interrupt=None):
    """Install exact frozen after-images; never accept or activate a graph."""
    if not isinstance(owner, access._InspectionOwner):
        raise access.MaintenanceError("Exclusive fixture owner required")
    state, project, path, reference, fingerprint = _handoff(owner, plan, approved_digest, plan_identity, clients_stopped)
    if action not in ("forward", "rollback"):
        raise ValueError("Unknown graph action")
    journal = project / access.CONTROL / ("graph-" + state["operation"] + ".sqlite3")
    stage = project / access.CONTROL / ("graph-" + state["operation"])
    header = {"project": str(project), "token": state["token"], "operation": state["operation"],
              "plan": reference, "sha256": approved_digest, "revision": plan_identity, "stat": list(fingerprint)}
    fresh = not os.path.lexists(journal)
    if fresh:
        if action != "forward":
            raise ValueError("Rollback needs original graph evidence")
        descriptor = os.open(journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
    info = journal.lstat()
    if journal.is_symlink() or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe graph journal")
    with closing(migration.readonly(path)) as frozen, closing(sqlite3.connect(journal)) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA synchronous=FULL")
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA journal_mode=DELETE")
        if fresh:
            database.executescript("CREATE TABLE meta(payload TEXT,direction TEXT); CREATE TABLE transforms(number INTEGER PRIMARY KEY,source TEXT UNIQUE,destination TEXT UNIQUE,evidence TEXT,state TEXT);")
            database.execute("INSERT INTO meta VALUES(?,'forward')", (renames._encode(header),))
            for number, row in enumerate(frozen.execute("SELECT f.*,t.schema,t.patch,t.after_sha256,t.after_bytes FROM files f LEFT JOIN transformations t USING(source) WHERE disposition='Transform' ORDER BY source")):
                evidence = dict(row)
                renames._validate_row(project, evidence, reference)
                if evidence["schema"] not in ("silhouette-v1", "manifest-v1", "engraving-v2") or not evidence["after_sha256"]:
                    raise ValueError("Missing or unsupported approved transformation")
                database.execute("INSERT INTO transforms VALUES(?,?,?,?,'planned')", (number, row["source"], row["destination"], renames._encode(evidence)))
            _commit(database, journal.parent, interrupt, "metadata-prepared")
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Graph journal integrity failure")
        headers = database.execute("SELECT * FROM meta").fetchall()
        if len(headers) != 1 or json.loads(headers[0]["payload"]) != header:
            raise ValueError("Graph journal binding mismatch")
        expected = frozen.execute("SELECT f.*,t.schema,t.patch,t.after_sha256,t.after_bytes FROM files f LEFT JOIN transformations t USING(source) WHERE disposition='Transform' ORDER BY source")
        for actual, row in zip(database.execute("SELECT * FROM transforms ORDER BY source"), expected, strict=True):
            if json.loads(actual["evidence"]) != dict(row) or actual["source"] != row["source"] or actual["destination"] != row["destination"]:
                raise ValueError("Graph journal differs from frozen mapping")
        if headers[0]["direction"] not in ("forward", "rollback") or action == "forward" and headers[0]["direction"] != "forward":
            raise ValueError("Rollback graph cannot resume forward")
        if action == "rollback" and headers[0]["direction"] != "rollback":
            database.execute("UPDATE meta SET direction='rollback'")
            _commit(database, journal.parent, interrupt, "rollback-start")
        if not stage.exists():
            os.mkdir(stage, 0o700)
        locations.project_file(project, journal)
        previous = -1 if action == "forward" else 2**63 - 1
        comparison, order = (">", "ASC") if action == "forward" else ("<", "DESC")
        while True:
            row = database.execute("SELECT * FROM transforms WHERE number" + comparison + "? ORDER BY number " + order + " LIMIT 1", (previous,)).fetchone()
            if row is None:
                break
            previous = row["number"]
            owner.require_fixture_renames()
            evidence = json.loads(row["evidence"])
            before = str((stage / (str(previous) + ".before")).relative_to(project))
            after = str((stage / (str(previous) + ".after")).relative_to(project))
            original = _exact(project, row["source"], evidence["sha256"])
            retained = _exact(project, before, evidence["sha256"])
            installed = _exact(project, row["destination"], evidence["after_sha256"])
            staged = _exact(project, after, evidence["after_sha256"])
            if row["state"] not in ("planned", "intent", "done", "undo-intent", "undone"):
                raise ValueError("Unknown transformation state")
            if action == "rollback":
                if installed and (not retained or original or staged):
                    raise ValueError("Ambiguous metadata rollback")
                _state(database, journal.parent, previous, "undo-intent", interrupt)
                if installed:
                    _relocate(project, row["destination"], after, evidence["after_sha256"])
                    renames._event(interrupt, "metadata-uninstalled")
                if retained:
                    if original:
                        raise ValueError("Original path collision")
                    _relocate(project, before, row["source"], evidence["sha256"])
                elif not original:
                    raise ValueError("Missing metadata before-image")
                _state(database, journal.parent, previous, "undone", interrupt)
                continue
            if row["state"] in ("undo-intent", "undone"):
                raise ValueError("Rollback graph cannot resume forward")
            if installed:
                if not retained or original or staged or row["state"] == "planned":
                    raise ValueError("Incomplete transformation evidence")
            else:
                if original == retained or row["state"] == "done":
                    raise ValueError("Ambiguous or incomplete metadata transformation")
                if not staged:
                    if not original:
                        raise ValueError("Missing staged after-image")
                    with access._root(project) as (_path, root):
                        with renames._parent(root, row["source"].split("/")) as (parent, name):
                            renames._verify(parent, name, evidence, original=True)
                    record = json.loads((project / row["source"]).read_bytes())
                    patch = json.loads(evidence["patch"])
                    result = {**record, **patch} if evidence["schema"] == "silhouette-v1" else patch
                    payload = migration.encode(result)
                    if migration.sha256(payload) != evidence["after_sha256"] or len(payload) != evidence["after_bytes"]:
                        raise ValueError("Approved transformation after-image mismatch")
                    renames._event(interrupt, "metadata-preparation")
                    from data.annotate import atomic_write_text
                    atomic_write_text(project / after, payload.decode("utf-8"))
                    descriptor = os.open(project / after, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                _state(database, journal.parent, previous, "intent", interrupt)
                if original:
                    _relocate(project, row["source"], before, evidence["sha256"])
                    renames._event(interrupt, "metadata-retained")
                _relocate(project, after, row["destination"], evidence["after_sha256"])
                renames._event(interrupt, "metadata-replaced")
            if not _exact(project, row["destination"], evidence["after_sha256"]) or not _exact(project, before, evidence["sha256"]):
                raise ValueError("Transformation completion content changed")
            _state(database, journal.parent, previous, "done", interrupt)
        return {"state": "metadata_rolled_back" if action == "rollback" else "metadata_installed",
                "graph_accepted": False, "registry_activated": False, "journal": str(journal)}


def _integrity(database):
    if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or database.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("SQLite integrity or foreign-key failure")


def _identity_equality(database, preparation):
    database.execute("ATTACH DATABASE ? AS expected_identities", (preparation.as_uri() + "?mode=ro",))
    try:
        for table, expected in (("instances", "identity_bindings"), ("shots", "shot_reservations")):
            for query in (f"SELECT * FROM main.{expected} EXCEPT SELECT * FROM expected_identities.{table}",
                          f"SELECT * FROM expected_identities.{table} EXCEPT SELECT * FROM main.{expected}"):
                if database.execute(query + " LIMIT 1").fetchone():
                    raise ValueError("Incorrect identity binding or high-water reservation")
    finally:
        database.execute("DETACH DATABASE expected_identities")


def _file_sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    sync_directory(path.parent)


def _retire_unsealed(project, database, journal, reference, interrupt):
    relative_path(project, reference)
    database.execute("CREATE TABLE IF NOT EXISTS unsealed_outputs(source TEXT PRIMARY KEY,sha256 TEXT NOT NULL,holding TEXT NOT NULL,state TEXT NOT NULL)")
    recorded = database.execute("SELECT * FROM unsealed_outputs WHERE source=?", (reference,)).fetchone()
    holding = reference + ".unsealed"
    if recorded is None:
        if not os.path.lexists(project / reference):
            return
        path = locations.project_file(project, reference)
        if path.stat().st_nlink != 1 or os.path.lexists(project / holding):
            raise ValueError("Unexpected unsealed recovery contents")
        database.execute("INSERT INTO unsealed_outputs VALUES(?,?,?,'intent')", (reference, digest(path), holding))
        _commit(database, journal.parent, interrupt, "unsealed-retain-intent")
        recorded = database.execute("SELECT * FROM unsealed_outputs WHERE source=?", (reference,)).fetchone()
    if recorded["holding"] != holding:
        raise ValueError("Unsealed recovery evidence differs")
    source = _exact(project, reference, recorded["sha256"])
    retained = _exact(project, holding, recorded["sha256"])
    if recorded["state"] == "retired":
        if source or not retained:
            raise ValueError("Repeated unsealed failure or changed retained evidence")
        return
    if recorded["state"] != "intent" or source == retained:
        raise ValueError("Ambiguous unsealed recovery evidence")
    if source:
        _relocate(project, reference, holding, recorded["sha256"])
    database.execute("UPDATE unsealed_outputs SET state='retired' WHERE source=?", (reference,))
    _commit(database, journal.parent, interrupt, "unsealed-retained")


def _retire_candidate(project, database, journal, candidate, interrupt):
    reference = str(candidate.relative_to(project))
    leftovers = []
    for item in candidate.parent.iterdir():
        if item.name.startswith("." + candidate.name + ".") and any(item.name.endswith(suffix) for suffix in (".tmp", ".tmp-journal", ".tmp-wal", ".tmp-shm")):
            leftovers.append(item)
            if len(leftovers) > 4:
                raise ValueError("Unbounded owner temporary-file recovery")
    temporary_databases = [item for item in leftovers if item.name.endswith(".tmp")]
    if len(temporary_databases) > 1 or (temporary_databases and os.path.lexists(candidate)):
        raise ValueError("Multiple unsealed owner candidates")
    prefix = str(candidate.parent.relative_to(project)) + "/." + candidate.name + "."
    if database.execute("SELECT 1 FROM sqlite_master WHERE name='unsealed_outputs'").fetchone():
        retained = {row[0] for row in database.execute("SELECT source FROM unsealed_outputs") if row[0] == reference or row[0].startswith(prefix) or row[0] in {reference + suffix for suffix in ("-journal", "-wal", "-shm")}}
        fresh = {str(item.relative_to(project)) for item in leftovers}
        if retained and fresh - retained:
            raise ValueError("Repeated owner temporary-file failure exceeds retention bound")
    for suffix in ("-journal", "-wal", "-shm", ""):
        _retire_unsealed(project, database, journal, reference + suffix, interrupt)
    for item in sorted(leftovers):
        _retire_unsealed(project, database, journal, str(item.relative_to(project)), interrupt)


def _validate_vocabulary(project, registry, reference):
    from services import vocabulary_index, derived_vocabulary
    name = Path(reference).name
    media_type = "gameplay" if "gameplay" in name else "movie"
    builder = derived_vocabulary.build_derived_vocabulary if name.endswith("-derived.json") else vocabulary_index.build_vocabulary_index
    expected = builder(str(project), media_type, force=True, registry=registry, write=False)
    actual = json.loads(locations.project_file(project, reference).read_bytes())
    for payload in (actual, expected):
        payload["meta"].pop("built_at", None)
    if actual != expected:
        raise ValueError("Vocabulary content/version/source association differs from canonical owner")


@_fixture_access
def install_fixture_registry(owner, plan, *, approved_digest, plan_identity, clients_stopped=False, interrupt=None):
    """Install the one canonical model while normal admission remains closed."""
    state, project, path, _, _ = _handoff(owner, plan, approved_digest, plan_identity, clients_stopped)
    journal = project / access.CONTROL / ("graph-" + state["operation"] + ".sqlite3")
    with closing(migration.readonly(path)) as frozen, closing(sqlite3.connect(journal)) as evidence:
        evidence.row_factory = sqlite3.Row
        evidence.execute("PRAGMA synchronous=FULL")
        if evidence.execute("SELECT 1 FROM transforms WHERE state!='done' LIMIT 1").fetchone():
            raise ValueError("Incomplete transformation")
        if evidence.execute("SELECT direction FROM meta").fetchone()[0] != "forward":
            raise ValueError("Graph rollback already started")
        metadata = {row[0]: json.loads(row[1]) for row in frozen.execute("SELECT * FROM meta")}
        proposal_reference = metadata["identity_registry"]
        preparation = locations.project_file(project, proposal_reference)
        expected = metadata.get("inputs", {}).get(proposal_reference)
        if not expected or digest(preparation) != expected:
            raise ValueError("Preparation registry frozen digest differs")
        adoption = migration.registry_adoption_plan(preparation)
        if adoption["upgrades"]:
            raise ValueError("Unsupported preparation registry upgrade: " + json.dumps(adoption["upgrades"]))
        _identity_equality(frozen, preparation)
        evidence.execute("CREATE TABLE IF NOT EXISTS registry_install(path TEXT PRIMARY KEY,baseline TEXT NOT NULL,state TEXT NOT NULL,sha256 TEXT)")
        candidate = project / "data/indexes" / (".graph-" + state["operation"] + ".sqlite3")
        target = project / "data/indexes/resources.sqlite3"
        stored = evidence.execute("SELECT * FROM registry_install").fetchall()
        if not stored:
            if os.path.lexists(candidate) or os.path.lexists(target):
                raise ValueError("Unexpected registry destination")
            evidence.execute("INSERT INTO registry_install VALUES(?,?,'intent',NULL)", (str(target.relative_to(project)), expected))
            _commit(evidence, journal.parent, interrupt, "registry-intent")
        elif len(stored) != 1 or stored[0]["baseline"] != expected or stored[0]["path"] != str(target.relative_to(project)):
            raise ValueError("Registry installation evidence differs")
        installed = stored and stored[0]["state"] == "installed"
        if installed:
            if not _exact(project, str(target.relative_to(project)), stored[0]["sha256"]):
                raise ValueError("Missing installed registry")
            _identity_equality(frozen, target)
            return {"registry": str(target), "graph_accepted": False}
        if target.exists():
            if candidate.exists() or not stored or stored[0]["state"] != "prepared" or not _exact(project, str(target.relative_to(project)), stored[0]["sha256"]):
                raise ValueError("Interrupted registry installation lacks verified sealing evidence")
            _identity_equality(frozen, target)
            _file_sync(target)
            evidence.execute("UPDATE registry_install SET state='installed'")
            _commit(evidence, journal.parent, interrupt, "registry-completion")
            return {"registry": str(target), "graph_accepted": False}
        if not candidate.exists():
            evidence.execute("UPDATE registry_install SET state='copying'")
            _commit(evidence, journal.parent, interrupt, "registry-copy-intent")
        elif stored and stored[0]["state"] == "copying":
            _retire_candidate(project, evidence, journal, candidate, interrupt)
        if not candidate.exists():
            renames._event(interrupt, "registry-preparation")
            create_storage_directory(project, candidate.parent)
            with closing(migration.readonly(preparation)) as source, closing(sqlite3.connect(candidate)) as output:
                source.backup(output)
                _integrity(output)
                output.commit()
            _file_sync(candidate)
            renames._event(interrupt, "registry-snapshot-created")
            evidence.execute("UPDATE registry_install SET state='populating'")
            _commit(evidence, journal.parent, interrupt, "registry-snapshot")
        _identity_equality(frozen, candidate)
        if stored and stored[0]["state"] == "prepared" and digest(candidate) != stored[0]["sha256"]:
            raise ValueError("Prepared registry content changed")
        store = ResourceStore(project, candidate, approved=True)
        for collection in frozen.execute("SELECT * FROM collections ORDER BY media_type,media_id"):
            store.import_fixture_collection(owner, frozen, collection)
        for kind, schema in (("silhouettes", "silhouette-v1"), ("engravings", "engraving-v2")):
            if kind == "engravings" and metadata.get("generation_run_plan"):
                store.import_fixture_run(owner, frozen, metadata["generation_run_plan"], interrupt=interrupt)
            for row in frozen.execute("SELECT f.instance_id,coalesce(f.mode,'') AS mode FROM files f JOIN transformations t USING(source) WHERE t.schema=? ORDER BY f.instance_id", (schema,)):
                store.import_fixture_pair(owner, frozen, row["instance_id"], kind, row["mode"])
                renames._event(interrupt, "registry-pair-committed")
        with closing(migration.readonly(candidate)) as checked:
            _integrity(checked)
        _file_sync(candidate)
        checksum = digest(candidate)
        evidence.execute("UPDATE registry_install SET state='prepared',sha256=?", (checksum,))
        _commit(evidence, journal.parent, interrupt, "registry-prepared")
        renames._event(interrupt, "registry-installation")
        _relocate(project, str(candidate.relative_to(project)), str(target.relative_to(project)), checksum)
        renames._event(interrupt, "registry-installed")
        evidence.execute("UPDATE registry_install SET state='installed'")
        _commit(evidence, journal.parent, interrupt, "registry-completion")
        return {"registry": str(target), "graph_accepted": False}


def _build_index(project, registry, reference, staging):
    from services import illustration_index, vocabulary_index, derived_vocabulary, corpus_stats, engraving_viability
    name = Path(reference).name
    if reference.startswith("data/indexes/illustration/"):
        media_type, remainder = name.split("-", 1)
        kind = remainder.split(".")[0]
        if media_type not in ("movie", "gameplay") or kind not in ("silhouettes", "engravings"):
            raise ValueError("Unknown Illustration transition")
        if name.endswith(".revision"):
            illustration_index.invalidate_index(project, kind, media_type, destination=staging)
        elif name.endswith(".sqlite3"):
            result = illustration_index.rebuild_index(project, kind, media_type, registry=registry, destination=staging)
            if result["status"] != "ready":
                raise ValueError("Illustration rebuild was not ready")
        else:
            raise ValueError("Unknown Illustration output")
    elif reference.startswith("data/vocabulary/"):
        media_type = "gameplay" if "gameplay" in name else "movie"
        if name == "vocabulary-" + media_type + "-derived.json":
            derived_vocabulary.build_derived_vocabulary(str(project), media_type, registry=registry, force=True, destination=staging)
        elif name in ("vocabulary-" + media_type + ".json", "vocabulary_" + media_type + ".json"):
            vocabulary_index.build_vocabulary_index(str(project), media_type, registry=registry, force=True, destination=staging)
        else:
            raise ValueError("Unknown vocabulary transition")
    elif reference == "data/indexes/corpus_stats.json":
        corpus_stats.refresh_corpus_stats_cache(str(project), registry=registry, destination=staging)
    elif reference == "data/indexes/viability/movie-silhouette-viability.sqlite3":
        from services.resource_resolution import indexed_publications
        records = indexed_publications(project, "silhouettes", "movie", registry=registry)
        metrics = engraving_viability.measure_records(project, records, media_type="movie", use_cache=False)
        if metrics:
            engraving_viability.store_cached_metrics(project, metrics, media_type="movie", destination=staging)
        else:
            engraving_viability._connect(staging).close()
    else:
        raise ValueError("No canonical owner for frozen dependent output: " + reference)


@_fixture_access
def rebuild_fixture_indexes(owner, plan, *, approved_digest, plan_identity, clients_stopped=False, interrupt=None):
    state, project, path, _, _ = _handoff(owner, plan, approved_digest, plan_identity, clients_stopped)
    journal = project / access.CONTROL / ("graph-" + state["operation"] + ".sqlite3")
    registry = project / "data/indexes/resources.sqlite3"
    with closing(migration.readonly(path)) as frozen, closing(sqlite3.connect(journal)) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA synchronous=FULL")
        installation = database.execute("SELECT * FROM registry_install").fetchone()
        if installation is None or installation["state"] != "installed" or not _exact(project, installation["path"], installation["sha256"]):
            raise ValueError("Interrupted or changed registry installation")
        if database.execute("SELECT direction FROM meta").fetchone()[0] != "forward":
            raise ValueError("Graph rollback already started")
        database.execute("CREATE TABLE IF NOT EXISTS index_outputs(source TEXT PRIMARY KEY,before_sha TEXT NOT NULL,after_sha TEXT,state TEXT NOT NULL,holding TEXT NOT NULL,staging TEXT NOT NULL)")
        root = project / "outputs/maintenance" / ("fixture-graph-" + state["operation"])
        create_storage_directory(project, root)
        planned = frozen.execute("SELECT f.* FROM files f JOIN index_transitions t USING(source) WHERE disposition='Rebuild' ORDER BY source").fetchall()
        if len(planned) != frozen.execute("SELECT count(*) FROM files WHERE disposition='Rebuild'").fetchone()[0]:
            raise ValueError("Missing dependent-index transition evidence")
        for number, row in enumerate(planned):
            stored = database.execute("SELECT * FROM index_outputs WHERE source=?", (row["source"],)).fetchone()
            holding = str((root / (str(number) + ".before")).relative_to(project))
            staging = str((root / (str(number) + ".new")).relative_to(project))
            if stored is None:
                database.execute("INSERT INTO index_outputs VALUES(?,?,NULL,'intent',?,?)", (row["source"], row["sha256"], holding, staging))
                _commit(database, journal.parent, interrupt, "index-retain-intent")
                stored = database.execute("SELECT * FROM index_outputs WHERE source=?", (row["source"],)).fetchone()
            if stored["before_sha"] != row["sha256"] or stored["holding"] != holding or stored["staging"] != staging:
                raise ValueError("Dependent-index evidence differs")
            if stored["state"] == "intent":
                original = _exact(project, row["source"], row["sha256"])
                retained = _exact(project, holding, row["sha256"])
                if original == retained:
                    raise ValueError("Ambiguous previous index output")
                if original:
                    _relocate(project, row["source"], holding, row["sha256"])
                database.execute("UPDATE index_outputs SET state='retained' WHERE source=?", (row["source"],))
                _commit(database, journal.parent, interrupt, "index-retained")
        priority = lambda item: (0 if item["source"].endswith(".revision") else 3 if item["source"].endswith("corpus_stats.json") else 1 if "/illustration/" in item["source"] else 2, item["source"])
        for row in sorted(planned, key=priority):
            owner.require_fixture_renames()
            stored = database.execute("SELECT * FROM index_outputs WHERE source=?", (row["source"],)).fetchone()
            if stored["state"] == "installed":
                if not _exact(project, stored["source"], stored["after_sha"]):
                    raise ValueError("Missing or changed installed index")
                continue
            if stored["state"] == "building":
                if os.path.lexists(project / stored["source"]):
                    raise ValueError("Unexpected unsealed final output; no overwrite permitted")
                if not _exact(project, stored["holding"], stored["before_sha"]):
                    raise ValueError("Missing previous output for interrupted build")
                _retire_candidate(project, database, journal, project / stored["staging"], interrupt)
                database.execute("UPDATE index_outputs SET state='retained' WHERE source=?", (row["source"],))
                _commit(database, journal.parent, interrupt, "index-empty-recovery")
                stored = database.execute("SELECT * FROM index_outputs WHERE source=?", (row["source"],)).fetchone()
            if stored["state"] == "retained":
                if os.path.lexists(project / stored["source"]) or os.path.lexists(project / stored["staging"]):
                    raise ValueError("Unexpected replacement index output")
                database.execute("UPDATE index_outputs SET state='building' WHERE source=?", (row["source"],))
                _commit(database, journal.parent, interrupt, "index-build-intent")
                renames._event(interrupt, "index-rebuilding")
                _build_index(project, registry, row["source"], project / stored["staging"])
                renames._event(interrupt, "index-staged")
                generated = project / stored["staging"]
                locations.project_file(project, generated)
                _file_sync(generated)
                checksum = digest(generated)
                database.execute("UPDATE index_outputs SET state='built',after_sha=? WHERE source=?", (checksum, row["source"]))
                _commit(database, journal.parent, interrupt, "index-built")
                stored = database.execute("SELECT * FROM index_outputs WHERE source=?", (row["source"],)).fetchone()
            if stored["state"] != "built":
                raise ValueError("Unknown dependent-index state")
            renames._event(interrupt, "index-replacement")
            staged = _exact(project, stored["staging"], stored["after_sha"])
            installed = _exact(project, stored["source"], stored["after_sha"])
            if staged == installed:
                raise ValueError("Ambiguous sealed index installation")
            if staged:
                _relocate(project, stored["staging"], stored["source"], stored["after_sha"])
            _file_sync(project / stored["source"])
            database.execute("UPDATE index_outputs SET state='installed' WHERE source=?", (row["source"],))
            _commit(database, journal.parent, interrupt, "index-installed")
        return {"indexes": len(planned), "graph_accepted": False}


@_fixture_access
def validate_fixture_graph(owner, plan, *, approved_digest, plan_identity, clients_stopped=False, interrupt=None):
    state, project, path, _, _ = _handoff(owner, plan, approved_digest, plan_identity, clients_stopped, verify_resources=False)
    from services.resource_validation import validate_metadata
    failures = []
    total = 0
    def fail(category, source, detail):
        nonlocal total
        total += 1
        if len(failures) < 128:
            failures.append({"category": category, "source": source, "detail": detail})
    journal = project / access.CONTROL / ("graph-" + state["operation"] + ".sqlite3")
    registry = project / "data/indexes/resources.sqlite3"
    try:
        with closing(migration.readonly(path)) as frozen, closing(migration.readonly(journal)) as evidence, closing(migration.readonly(registry)) as installed:
            renames._event(interrupt, "graph-validation")
            for database in (frozen, evidence, installed):
                _integrity(database)
            try:
                _identity_equality(frozen, registry)
            except ValueError as error:
                fail("incorrect_identity", "registry", str(error))
            if evidence.execute("SELECT direction FROM meta").fetchone()[0] != "forward":
                fail("interrupted_installation", "journal", "rollback has started")
            installation = evidence.execute("SELECT * FROM registry_install").fetchone()
            if installation is None or installation["state"] != "installed" or digest(registry) != installation["sha256"]:
                fail("interrupted_installation", str(registry), "registry seal missing or changed")
            if evidence.execute("SELECT 1 FROM transforms WHERE state!='done' LIMIT 1").fetchone():
                fail("incomplete_transformation", "journal", "unfinished metadata")
            for row in frozen.execute("SELECT f.*,t.after_sha256 FROM files f LEFT JOIN transformations t USING(source) ORDER BY source"):
                if row["disposition"] not in ("Move", "Transform", "Retain"):
                    continue
                reference = row["source"] if row["disposition"] == "Retain" else row["destination"]
                checksum = row["after_sha256"] if row["disposition"] == "Transform" else row["sha256"]
                try:
                    if not _exact(project, reference, checksum):
                        fail("missing_resource", reference, "expected mapped file absent")
                except (ValueError, OSError) as error:
                    fail("changed_content", reference, str(error))
            expected_pairs = frozen.execute("SELECT count(*) FROM transformations WHERE schema IN ('silhouette-v1','engraving-v2')").fetchone()[0]
            if installed.execute("SELECT count(*) FROM resource_locations").fetchone()[0] != expected_pairs:
                fail("incorrect_identity", "registry", "publication membership count differs")
            if installed.execute("SELECT count(*) FROM annotation_layouts").fetchone()[0] != frozen.execute("SELECT count(*) FROM collections").fetchone()[0]:
                fail("stale_reference", "annotation_layouts", "collection membership differs")
            metadata = {row[0]: json.loads(row[1]) for row in frozen.execute("SELECT * FROM meta")}
            preparation = locations.project_file(project, metadata["identity_registry"])
            if digest(preparation) != metadata["inputs"][metadata["identity_registry"]]:
                raise ValueError("Frozen preparation registry changed")
            with closing(migration.readonly(preparation)) as baseline:
                prior_revisions = {(row[0], row[1]): row[2] for row in baseline.execute("SELECT media_type,media_id,revision FROM annotation_layouts")}
            for collection in frozen.execute("SELECT * FROM collections"):
                current = installed.execute("SELECT * FROM annotation_layouts WHERE media_type=? AND media_id=?", (collection["media_type"], collection["media_id"])).fetchone()
                expected_artifacts = {}
                for suffix, artifact in json.loads(collection["artifacts"]).items():
                    member = frozen.execute("SELECT f.sha256,t.after_sha256,f.disposition FROM files f LEFT JOIN transformations t USING(source) WHERE source=?", (artifact["source"],)).fetchone()
                    if member:
                        expected_artifacts[suffix] = member["after_sha256"] if member["disposition"] == "Transform" else member["sha256"]
                expected_revision = prior_revisions.get((collection["media_type"], collection["media_id"]), 0) + 1
                if current is None or current["layout"] != "media-first-v1" or current["filename"] != collection["filename"] or json.loads(current["artifacts"]) != expected_artifacts or current["revision"] != expected_revision:
                    fail("stale_reference", collection["filename"], "incomplete or incorrect collection commitment")
                try:
                    from data.media_id import parse_shot_id
                    from services.frame_match import best_frame_path
                    annotation = locations.resolve_annotation(project, collection["filename"], collection["media_type"], registry=registry)
                    shots = json.loads(annotation.read_bytes())
                    seen = set()
                    for entry in shots:
                        shot = entry["shot"]
                        shot_id = shot["shot_id"]
                        media_id, start, end = parse_shot_id(shot_id)
                        if shot_id in seen or media_id != collection["media_id"]:
                            raise ValueError("Duplicate or misdirected annotation shot")
                        seen.add(shot_id)
                        best = shot.get("best_frame")
                        if best:
                            if type(best.get("frame")) is not int or not start <= best["frame"] <= end:
                                raise ValueError("Incorrect BEST selection")
                            best_path = best_frame_path(str(project), collection["media_type"], collection["filename"], shot_id)
                            best_reference = str(best_path.relative_to(project))
                            member = frozen.execute("SELECT sha256 FROM files WHERE source=? AND disposition='Retain'", (best_reference,)).fetchone()
                            if member is None or not _exact(project, best_reference, member[0]):
                                raise ValueError("Missing or changed frozen BEST PNG")
                except (ValueError, KeyError, TypeError, OSError) as error:
                    fail("stale_reference", collection["filename"], str(error))
            for row in installed.execute("SELECT * FROM resource_locations ORDER BY instance_id,kind,mode"):
                try:
                    binding = locations.registered_instance(project, registry, row["instance_id"])
                    publication = locations.current_location(project, binding, row["kind"], row["mode"])
                    expected = frozen.execute("SELECT f.destination,t.after_sha256 FROM files f JOIN transformations t USING(source) WHERE f.instance_id=? AND coalesce(f.mode,'')=? AND t.schema=?", (row["instance_id"], row["mode"], "silhouette-v1" if row["kind"] == "silhouettes" else "engraving-v2")).fetchall()
                    if len(expected) != 1 or expected[0]["destination"] != row["record_path"] or expected[0]["after_sha256"] != row["record_sha256"]:
                        fail("incorrect_location", row["record_path"], "registry does not match frozen pair")
                    collection = installed.execute("SELECT layout FROM annotation_layouts WHERE media_type=? AND media_id=?", (binding["media_type"], binding["media_id"])).fetchone()
                    if collection is None or collection[0] != "media-first-v1":
                        fail("stale_reference", row["record_path"], "canonical annotation commitment missing; legacy fallback forbidden")
                    findings = validate_metadata(project, binding, publication["record"], row["kind"], row["mode"], annotation_layout=collection[0] if collection else "legacy-v1", preserve_assignment=True)
                    if findings["state"] != "valid":
                        fail("incorrect_identity", row["record_path"], json.dumps(findings, default=str))
                except (ValueError, OSError, KeyError) as error:
                    fail("incorrect_location", row["record_path"], str(error))
            for row in frozen.execute("SELECT * FROM reference_plan WHERE required=1"):
                target = row["proposed_target"]
                try:
                    if not target:
                        raise ValueError("unresolved approved reference")
                    locations.project_file(project, target)
                    source = frozen.execute("SELECT destination,disposition FROM files WHERE source=?", (row["source"],)).fetchone()
                    reference = source["destination"] if source and source["disposition"] in ("Move", "Transform") else row["source"]
                    record = json.loads(locations.project_file(project, reference).read_bytes())
                    historical_key = row["pointer"].removeprefix("/source/")
                    if record.get("resource_kind") == "engravings" and historical_key in ("silhouette_json", "silhouette_png", "source_json"):
                        if record.get("historical_source", {}).get(historical_key) != row["target"]:
                            raise ValueError("Historical source provenance differs")
                        binding = locations.registered_instance(project, registry, record["source_instance_id"])
                        pair = locations.current_location(project, binding, "silhouettes", "")
                        if Path(target) not in (pair["metadata"], pair["image"], pair["metadata"].relative_to(project), pair["image"].relative_to(project)):
                            raise ValueError("Canonical source identity resolves elsewhere")
                        continue
                    value = record
                    for part in row["pointer"].split("/")[1:]:
                        key = part.replace("~1", "/").replace("~0", "~")
                        value = value[int(key)] if isinstance(value, list) and key.isdecimal() else value[key]
                    if row["pointer"] == "/png":
                        value = str((Path(reference).parent / value))
                    if Path(str(value)).is_absolute():
                        value = str(Path(value).relative_to(project))
                    expected = str(Path(target).relative_to(project)) if Path(target).is_absolute() else target
                    if value != expected:
                        raise ValueError("active reference differs from approved target")
                except (ValueError, OSError, KeyError, TypeError) as error:
                    fail("stale_reference", row["source"], str(error))
            indexes = {row["source"]: row for row in evidence.execute("SELECT * FROM index_outputs")}
            if len(indexes) != 15:
                fail("invalid_index", "index_outputs", "frozen fifteen-output cohort incomplete")
            for row in frozen.execute("SELECT * FROM files WHERE disposition='Rebuild'"):
                recorded = indexes.get(row["source"])
                try:
                    if recorded is None or recorded["state"] != "installed" or not _exact(project, row["source"], recorded["after_sha"]):
                        raise ValueError("missing/incomplete dependent index")
                    if row["source"].endswith(".sqlite3"):
                        with closing(migration.readonly(project / row["source"])) as index:
                            _integrity(index)
                            if "/illustration/" in row["source"]:
                                media_type, remainder = Path(row["source"]).name.split("-", 1)
                                kind = remainder.split(".")[0]
                                from services.illustration_index import load_index
                                if load_index(project, kind, media_type).get("status") != "ready":
                                    raise ValueError("Illustration owner reports a stale or invalid index")
                                expected = {item[0] for item in installed.execute("SELECT l.instance_id FROM resource_locations l JOIN instances i USING(instance_id) WHERE l.kind=? AND i.media_type=?", (kind, media_type))}
                                actual = []
                                for item in index.execute("SELECT payload FROM records"):
                                    payload = json.loads(item[0])
                                    from services.illustration_index import _deserialize_record
                                    resolved = _deserialize_record(project, item[0])
                                    binding = locations.registered_instance(project, registry, resolved["instance_id"])
                                    if any(resolved.get(key) != binding[key] for key in ("media_id", "media_type", "shot_id")) or resolved.get("resource_registry") != "data/indexes/resources.sqlite3":
                                        raise ValueError("index source/navigation association differs")
                                    actual.append(payload.get("instance_id") or payload.get("source_instance_id"))
                                if len(actual) != len(expected) or set(actual) != expected:
                                    raise ValueError("index canonical identity membership differs")
                            elif "/viability/" in row["source"]:
                                from services.resource_resolution import indexed_publications
                                from services import engraving_viability
                                records = indexed_publications(project, "silhouettes", "movie", registry=registry)
                                expected = {relative_path(project, item["path"]): engraving_viability.measure_silhouette(item["output_png"]) for item in records}
                                expected = {key: value for key, value in expected.items() if value is not None}
                                actual = {}
                                for key, version, metrics in index.execute("SELECT record_path,metrics_version,metrics FROM viability"):
                                    if version != engraving_viability.METRICS_VERSION:
                                        raise ValueError("Stale viability metrics version")
                                    actual[key] = json.loads(metrics)
                                if actual != expected:
                                    raise ValueError("Viability membership/metrics differ from canonical masks")
                    elif row["source"] == "data/indexes/corpus_stats.json":
                        from services.corpus_stats import get_corpus_stats_state
                        if get_corpus_stats_state(str(project), registry=registry).get("state") != "ready":
                            raise ValueError("Corpus-stat owner reports a stale or invalid cache")
                    elif row["source"].startswith("data/vocabulary/"):
                        _validate_vocabulary(project, registry, row["source"])
                except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
                    fail("invalid_index", row["source"], str(error))
            for kind in ("silhouettes", "engravings"):
                for media_type in ("movie", "gameplay"):
                    root = project / "data" / kind / media_type
                    for parent, directories, filenames in os.walk(root, followlinks=False):
                        for name in (*directories, *filenames):
                            candidate = Path(parent) / name
                            if candidate.is_symlink():
                                fail("unexpected_resource", str(candidate.relative_to(project)), "symlink in canonical graph")
                        for name in filenames:
                            candidate = str((Path(parent) / name).relative_to(project))
                            if frozen.execute("SELECT 1 FROM files WHERE destination=?", (candidate,)).fetchone() is None:
                                run = installed.execute("SELECT 1 FROM generation_runs WHERE run_path=? AND state='complete'", (candidate,)).fetchone()
                                if run is None:
                                    fail("unexpected_resource", candidate, "not in approved graph")
            run = metadata.get("generation_run_plan")
            if run:
                source = locations.registered_instance(project, registry, run["instance_id"])
                silhouette = locations.current_location(project, source, "silhouettes", "")
                rows = installed.execute("SELECT * FROM generation_runs WHERE instance_id=? AND provider=? AND number=?", (run["instance_id"], run["provider"], run["proposed_number"])).fetchall()
                if len(rows) != 1 or rows[0]["state"] != "complete" or rows[0]["mode"] != run["mode"] or rows[0]["run_sha256"] != run["wrapper_sha256"] or rows[0]["run_path"] != run["destination"] or json.loads(rows[0]["manifest"]) != run["artifact_hashes"] or not _exact(project, run["destination"], run["wrapper_sha256"]):
                    fail("stale_reference", "generation_run", "historical run commitment differs")
                if run["payload"]["source"]["image_sha256"] != digest(silhouette["image"]):
                    fail("stale_reference", "generation_run", "source mask association differs")
            marker = project / access.CONTROL / access.CANONICAL_CONTEXT
            if marker.exists():
                try:
                    if access.canonical_registry(project) != registry:
                        raise ValueError("Wrong canonical consumer context")
                except (ValueError, access.MaintenanceError) as error:
                    fail("stale_reference", "consumer_context", str(error))
    except (ValueError, OSError, KeyError, TypeError, IndexError, sqlite3.Error, access.MaintenanceError) as error:
        fail("interrupted_installation", "graph", str(error))
    return {"graph_accepted": total == 0, "failure_count": total, "failures": failures, "ordinary_consumers_enabled": False}


@_fixture_access
def install_fixture_graph(owner, plan, *, approved_digest, plan_identity, clients_stopped=False, interrupt=None):
    arguments = {"approved_digest": approved_digest, "plan_identity": plan_identity, "clients_stopped": clients_stopped, "interrupt": interrupt}
    transform_fixture_metadata(owner, plan, **arguments)
    install_fixture_registry(owner, plan, **arguments)
    rebuild_fixture_indexes(owner, plan, **arguments)
    result = validate_fixture_graph(owner, plan, **arguments)
    if not result["graph_accepted"]:
        raise ValueError("Graph validation failed: " + json.dumps(result))
    state = owner.require_fixture_renames()
    journal = Path(state["project"]) / access.CONTROL / ("graph-" + state["operation"] + ".sqlite3")
    project = Path(state["project"])
    context = {"version": 1, "project": str(project), "device": state["device"], "inode": state["inode"],
               "state": "task3-qualified-awaiting-task4", "registry": "data/indexes/resources.sqlite3",
               "registry_sha256": digest(project / "data/indexes/resources.sqlite3"),
               "journal": str(journal.relative_to(project)), "plan_sha256": approved_digest,
               "operation": state["operation"], "plan_identity": plan_identity}
    with closing(migration.readonly(journal)) as database:
        context["graph_sha256"] = migration.sha256(renames._encode([approved_digest, context["registry_sha256"],
            [list(row) for row in database.execute("SELECT source,after_sha FROM index_outputs ORDER BY source")]]).encode())
    result["canonical_context"] = context
    with closing(sqlite3.connect(journal)) as database:
        database.execute("PRAGMA synchronous=FULL")
        database.execute("CREATE TABLE IF NOT EXISTS acceptance(payload TEXT NOT NULL)")
        database.execute("DELETE FROM acceptance")
        database.execute("INSERT INTO acceptance VALUES(?)", (renames._encode(result),))
        _commit(database, journal.parent, interrupt, "final-acceptance")
    renames._event(interrupt, "context-installation")
    with access._root(project) as (_path, root), access._control(root) as control:
        if access._exists(control, access.CANONICAL_CONTEXT):
            if access._read(control, access.CANONICAL_CONTEXT) != context:
                raise ValueError("Unexpected canonical context commitment")
        else:
            access._write(control, access.CANONICAL_CONTEXT, context)
    renames._event(interrupt, "context-installed")
    checked = validate_fixture_graph(owner, plan, **{**arguments, "interrupt": None})
    if not checked["graph_accepted"]:
        checked["canonical_context"] = context
        with closing(sqlite3.connect(journal)) as database:
            database.execute("PRAGMA synchronous=FULL")
            database.execute("UPDATE acceptance SET payload=?", (renames._encode(checked),))
            _commit(database, journal.parent, None, "final-acceptance-rejected")
        raise ValueError("Graph changed at final acceptance: " + json.dumps(checked))
    if access.canonical_registry(project) != project / context["registry"]:
        raise ValueError("Canonical consumer context did not resolve")
    return result


def _observed(project, reference, allowed):
    if not os.path.lexists(project / reference):
        return None
    path = locations.project_file(project, reference)
    checksum = digest(path)
    if path.stat().st_nlink != 1 or checksum not in allowed:
        raise ValueError("Unexpected or modified rollback content: " + reference)
    return checksum


@_fixture_access
def rollback_fixture_graph(owner, plan, *, approved_digest, plan_identity, clients_stopped=False, interrupt=None):
    state, project, path, _, _ = _handoff(owner, plan, approved_digest, plan_identity, clients_stopped)
    journal = project / access.CONTROL / ("graph-" + state["operation"] + ".sqlite3")
    with closing(sqlite3.connect(journal)) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA synchronous=FULL")
        _integrity(database)
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = database.execute("SELECT * FROM index_outputs ORDER BY source DESC").fetchall() if "index_outputs" in tables else []
        installation = database.execute("SELECT * FROM registry_install").fetchone() if "registry_install" in tables else None
        if installation is not None and installation["state"] not in ("intent", "copying", "populating", "prepared", "installed", "rolled_back"):
            raise ValueError("Unknown registry installation state")
        for row in indexes:
            if row["state"] == "building":
                if os.path.lexists(project / row["source"]):
                    raise ValueError("Unexpected unsealed final output; rollback blocked")
                _retire_candidate(project, database, journal, project / row["staging"], interrupt)
            _observed(project, row["source"], {row["before_sha"], row["after_sha"]})
            _observed(project, row["holding"], {row["before_sha"]})
            _observed(project, row["staging"], {row["after_sha"]})
        if installation and installation["state"] in ("intent", "copying", "populating"):
            if os.path.lexists(project / installation["path"]):
                raise ValueError("Unexpected unsealed registry destination")
            candidate = project / "data/indexes" / (".graph-" + state["operation"] + ".sqlite3")
            _retire_candidate(project, database, journal, candidate, interrupt)
            database.execute("UPDATE registry_install SET state='rolled_back',sha256=NULL")
            _commit(database, journal.parent, interrupt, "rollback-registry-candidate")
            installation = database.execute("SELECT * FROM registry_install").fetchone()
        if installation and installation["sha256"] is not None:
            _observed(project, installation["path"], {installation["sha256"]})
        database.execute("UPDATE meta SET direction='rollback'")
        marker = access.CONTROL + "/" + access.CANONICAL_CONTEXT
        database.execute("CREATE TABLE IF NOT EXISTS context_reversal(sha256 TEXT NOT NULL,holding TEXT NOT NULL,state TEXT NOT NULL)")
        reversal = database.execute("SELECT * FROM context_reversal").fetchone()
        if os.path.lexists(project / marker):
            receipt = database.execute("SELECT payload FROM acceptance").fetchone() if "acceptance" in tables else None
            context = json.loads(receipt[0]).get("canonical_context") if receipt else None
            expected_marker = migration.sha256((json.dumps(context, sort_keys=True, allow_nan=False) + "\n").encode())
            if context is None or not _exact(project, marker, expected_marker):
                raise ValueError("Canonical context reversal evidence differs")
            holding = access.CONTROL + "/graph-" + state["operation"] + "/context.after"
            if reversal is None:
                database.execute("INSERT INTO context_reversal VALUES(?,?,'intent')", (expected_marker, holding))
                _commit(database, journal.parent, interrupt, "context-reversal-intent")
            elif reversal["sha256"] != expected_marker or reversal["holding"] != holding or reversal["state"] != "intent":
                raise ValueError("Changed context reversal evidence")
            _relocate(project, marker, holding, expected_marker)
            renames._event(interrupt, "context-retired")
            database.execute("UPDATE context_reversal SET state='retired'")
            _commit(database, journal.parent, interrupt, "context-reversed")
        elif reversal is not None:
            if not _exact(project, reversal["holding"], reversal["sha256"]):
                raise ValueError("Missing retained canonical context")
            database.execute("UPDATE context_reversal SET state='retired'")
            _commit(database, journal.parent, interrupt, "context-reversed")
        if "acceptance" in tables:
            database.execute("DELETE FROM acceptance")
        _commit(database, journal.parent, interrupt, "graph-rollback-start")
        for row in indexes:
            current = _observed(project, row["source"], {row["before_sha"], row["after_sha"]})
            if current == row["after_sha"] and current is not None:
                if os.path.lexists(project / row["staging"]):
                    raise ValueError("Rollback replacement holding collision")
                _relocate(project, row["source"], row["staging"], row["after_sha"])
                renames._event(interrupt, "rollback-index-retired")
                current = None
            if current is None:
                if not _exact(project, row["holding"], row["before_sha"]):
                    raise ValueError("Missing previous index before-image")
                _relocate(project, row["holding"], row["source"], row["before_sha"])
            database.execute("UPDATE index_outputs SET state='undone' WHERE source=?", (row["source"],))
            _commit(database, journal.parent, interrupt, "rollback-index")
        if installation and installation["sha256"] is not None:
            holding = access.CONTROL + "/graph-" + state["operation"] + "/registry.after"
            current = _observed(project, installation["path"], {installation["sha256"]})
            retained = _observed(project, holding, {installation["sha256"]})
            candidate = "data/indexes/.graph-" + state["operation"] + ".sqlite3"
            staged = _observed(project, candidate, {installation["sha256"]})
            if sum(value is not None for value in (current, retained, staged)) != 1:
                raise ValueError("Ambiguous registry rollback")
            if current is not None:
                _relocate(project, installation["path"], holding, installation["sha256"])
                renames._event(interrupt, "rollback-registry-retired")
            elif staged is not None:
                _relocate(project, candidate, holding, installation["sha256"])
            database.execute("UPDATE registry_install SET state='rolled_back'")
            _commit(database, journal.parent, interrupt, "rollback-registry")
        with closing(migration.readonly(path)) as frozen:
            run_row = frozen.execute("SELECT value FROM meta WHERE key='generation_run_plan'").fetchone()
            if run_row:
                run = json.loads(run_row[0])
                holding = access.CONTROL + "/graph-" + state["operation"] + "/historical-run.after"
                current = _observed(project, run["destination"], {run["wrapper_sha256"]})
                retained = _observed(project, holding, {run["wrapper_sha256"]})
                if current is None and retained is None and (installation is None or installation["sha256"] is None):
                    run = None
                elif (current is None) == (retained is None):
                    raise ValueError("Ambiguous historical wrapper rollback")
                if run is None:
                    run_row = None
            if run_row:
                database.execute("CREATE TABLE IF NOT EXISTS run_reversal(checksum TEXT NOT NULL,state TEXT NOT NULL)")
                if not database.execute("SELECT 1 FROM run_reversal").fetchone():
                    database.execute("INSERT INTO run_reversal VALUES(?,'intent')", (run["wrapper_sha256"],))
                _commit(database, journal.parent, interrupt, "rollback-run-intent")
                if current:
                    _relocate(project, run["destination"], holding, run["wrapper_sha256"])
                database.execute("UPDATE run_reversal SET state='undone'")
                _commit(database, journal.parent, interrupt, "rollback-run")
    transform_fixture_metadata(owner, plan, approved_digest=approved_digest, plan_identity=plan_identity,
                               clients_stopped=clients_stopped, action="rollback", interrupt=interrupt)
    return {"task3_reversed": True, "graph_accepted": False, "task2_rollback_required": True,
            "maintenance_released": False, "complete_migration_rollback": False}