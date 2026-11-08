"""Opt-in authoritative registry storage. No production activation on import.

Identity tables are adopted unchanged, never allocated here. Location, annotation
layout and publication-recovery state share that same registry. Reads use shared
locks; approved writes use exclusive locks and rollback-journal transactions.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import re
import threading

from data import resource_locations as locations
from data.media_id import parse_instance_id, parse_shot_id, INSTANCE_REGISTRATION_POLICY


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_storage_directory(project, path, *, exclusive=False):
    """Explicit write helper: durably create each missing directory entry."""
    relative_path(project, path)
    path = Path(path)
    pending = []
    current = path
    while not current.exists():
        pending.append(current)
        current = current.parent
    if exclusive and not pending:
        raise FileExistsError("Storage operation directory already exists")
    for directory in reversed(pending):
        directory.mkdir(exist_ok=not (exclusive and directory == path))
        sync_directory(directory.parent)


def _registry_metadata(project, path, *, check_integrity=False):
    path = locations.project_file(project, path)
    with path.open("rb") as stream:
        header = stream.read(100)
    if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise ValueError("Registry requires a committed rollback-journal SQLite snapshot")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        meta = dict(database.execute("SELECT key,value FROM meta"))
        if meta.get("schema_version") != "1" or meta.get("project") != str(Path(project).resolve()):
            raise ValueError("Registry schema/project mismatch")
        if check_integrity and (database.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                    or database.execute("PRAGMA foreign_key_check").fetchone()):
            raise ValueError("Registry integrity/binding failure")
        return meta


def relative_path(project, path):
    """Validate prospective paths too; never creates directories."""
    root = Path(project).resolve(strict=True)
    path = Path(path)
    path = path if path.is_absolute() else root / path
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        locations.component(part)
        current /= part
        if current.is_symlink():
            raise ValueError("Symlink storage path is forbidden")
    if not relative.parts:
        raise ValueError("Resource path must not be project root")
    return relative.as_posix()


_registry_lock_state = threading.local()


@contextmanager
def registry_lock(project, registry, *, exclusive=False):
    path = locations.project_file(project, registry)
    held = getattr(_registry_lock_state, "held", {})
    key = (os.getpid(), str(path))
    if key in held:
        if exclusive and not held[key]:
            raise ValueError("Shared registry lock cannot be implicitly upgraded")
        yield
        return
    with path.open("rb") as stream:
        header = stream.read(100)
    if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise ValueError("Registry requires a committed rollback-journal SQLite snapshot")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        held[key] = exclusive
        _registry_lock_state.held = held
        if not exclusive:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
                if database.execute("SELECT 1 FROM sqlite_master WHERE name='publication_operations'").fetchone():
                    if database.execute("SELECT 1 FROM publication_operations WHERE state IN ('installing','files_published') OR (kind='annotation-collection' AND state='committed') LIMIT 1").fetchone():
                        raise ValueError("Explicit publication recovery required before lookup")
        yield
    finally:
        held.pop(key, None)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _install_schema(database):
    database.executescript("""
    CREATE TABLE resource_locations(
      instance_id TEXT NOT NULL REFERENCES instances(instance_id),
      kind TEXT NOT NULL CHECK(kind IN ('silhouettes','engravings')),
      mode TEXT NOT NULL CHECK((kind='silhouettes' AND mode='') OR
                              (kind='engravings' AND mode IN ('frame','isolated'))),
      layout TEXT NOT NULL CHECK(layout='media-first-v1'),
      revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>0),
      record_path TEXT NOT NULL UNIQUE CHECK(record_path LIKE 'data/'||kind||'/%'),
      image_path TEXT NOT NULL UNIQUE CHECK(image_path LIKE 'data/'||kind||'/%'),
      field TEXT NOT NULL CHECK(length(trim(field))>0),
      label TEXT NOT NULL CHECK(length(trim(label))>0),
    record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64 AND record_sha256 NOT GLOB '*[^0-9a-f]*'),
    image_sha256 TEXT NOT NULL CHECK(length(image_sha256)=64 AND image_sha256 NOT GLOB '*[^0-9a-f]*'),
      PRIMARY KEY(instance_id,kind,mode),
      CHECK(substr(record_path,1,1)!='/' AND instr(record_path,char(92))=0 AND instr('/'||record_path||'/','/../')=0),
      CHECK(substr(image_path,1,1)!='/' AND instr(image_path,char(92))=0 AND instr('/'||image_path||'/','/../')=0));
    CREATE TRIGGER increasing_location_revision BEFORE UPDATE ON resource_locations
      WHEN NEW.revision!=OLD.revision+1 OR NEW.instance_id!=OLD.instance_id OR NEW.kind!=OLD.kind OR NEW.mode!=OLD.mode
      BEGIN SELECT RAISE(ABORT,'Immutable ownership or non-monotonic location revision'); END;
    CREATE TRIGGER no_location_deletion BEFORE DELETE ON resource_locations
      BEGIN SELECT RAISE(ABORT,'Use authorized resource lifecycle retirement'); END;
    CREATE TABLE adoption_exceptions(reference TEXT PRIMARY KEY,reason TEXT NOT NULL,payload TEXT NOT NULL);
    CREATE TABLE annotation_layouts(
      media_type TEXT NOT NULL CHECK(media_type IN ('movie','gameplay')),media_id TEXT NOT NULL,
      filename TEXT NOT NULL,layout TEXT NOT NULL CHECK(layout IN ('legacy-v1','media-first-v1')),
      revision INTEGER NOT NULL CHECK(revision>0),artifacts TEXT NOT NULL,
      PRIMARY KEY(media_type,media_id));
    CREATE TRIGGER increasing_annotation_revision BEFORE UPDATE ON annotation_layouts
      WHEN NEW.revision!=OLD.revision+1 OR NEW.media_id!=OLD.media_id OR NEW.media_type!=OLD.media_type
      BEGIN SELECT RAISE(ABORT,'Non-monotonic annotation layout revision'); END;
    CREATE TABLE publication_operations(
            operation_id TEXT PRIMARY KEY,instance_id TEXT REFERENCES instances(instance_id),
      kind TEXT NOT NULL,mode TEXT NOT NULL,state TEXT NOT NULL
        CHECK(state IN ('prepared','installing','files_published','committed','finished','rolled_back')),
      details TEXT NOT NULL);
    CREATE UNIQUE INDEX one_pending_publication ON publication_operations(instance_id,kind,mode)
      WHERE state NOT IN ('finished','rolled_back');
    CREATE TABLE classification_corrections(
      correction_id TEXT PRIMARY KEY,reference TEXT NOT NULL,before_sha256 TEXT NOT NULL,
      field TEXT NOT NULL,approval TEXT NOT NULL,details TEXT NOT NULL);
        CREATE TABLE generation_runs(
            instance_id TEXT NOT NULL REFERENCES instances(instance_id),provider TEXT NOT NULL CHECK(provider IN ('local','openai')),
            number INTEGER NOT NULL CHECK(typeof(number)='integer' AND number>0),mode TEXT NOT NULL CHECK(mode IN ('frame','isolated')),
            state TEXT NOT NULL CHECK(state IN ('reserved','failed','abandoned','prepared','complete')),run_path TEXT NOT NULL UNIQUE,
            run_sha256 TEXT NOT NULL,manifest TEXT NOT NULL,stage TEXT NOT NULL,reservation TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(instance_id,provider,number));
        CREATE TRIGGER immutable_complete_run BEFORE UPDATE ON generation_runs
            WHEN OLD.state='complete' BEGIN SELECT RAISE(ABORT,'Published generation runs are immutable'); END;
        CREATE TRIGGER no_run_deletion BEFORE DELETE ON generation_runs
            BEGIN SELECT RAISE(ABORT,'Generation reservations cannot be recycled'); END;
    """)
    database.executemany("INSERT INTO meta VALUES(?,?)", [("location_schema_version", "1"), ("storage_schema_version", "1")])


def upgrade_collection_journal(project, registry, *, approved=False):
    """Explicitly allow media-owned operations in the existing publication journal."""
    store = ResourceStore(project, registry, approved=approved)
    with store.transaction() as database:
        column = next(row for row in database.execute("PRAGMA table_info(publication_operations)")
                      if row["name"] == "instance_id")
        if not column["notnull"]:
            return {"upgraded": False}
        if database.execute("SELECT 1 FROM publication_operations WHERE state NOT IN ('finished','rolled_back')").fetchone():
            raise ValueError("Pending publication requires recovery before journal upgrade")
        database.execute("DROP INDEX one_pending_publication")
        database.execute("ALTER TABLE publication_operations RENAME TO previous_publication_operations")
        database.execute("""CREATE TABLE publication_operations(
            operation_id TEXT PRIMARY KEY,instance_id TEXT REFERENCES instances(instance_id),
            kind TEXT NOT NULL,mode TEXT NOT NULL,state TEXT NOT NULL
            CHECK(state IN ('prepared','installing','files_published','committed','finished','rolled_back')),
            details TEXT NOT NULL)""")
        database.execute("INSERT INTO publication_operations SELECT * FROM previous_publication_operations")
        database.execute("DROP TABLE previous_publication_operations")
        database.execute("""CREATE UNIQUE INDEX one_pending_publication ON publication_operations(instance_id,kind,mode)
            WHERE state NOT IN ('finished','rolled_back')""")
        return {"upgraded": True}


def adopt_registry(project, proposal, destination, *, exceptions, approved=False, interrupt=None):
    """Explicit whole-snapshot adoption, never install/allocate during lookup.

    exceptions must be supplied, including an explicit empty list for fixtures.
    Interrupted unpublished copies can be retried; an existing adoption is never
    overwritten. Immutable original bindings/high-water reservations are retained.
    """
    if not approved:
        raise ValueError("Explicit registry adoption approval required")
    source = locations.project_file(project, proposal)
    target_relative = relative_path(project, destination)
    if not target_relative.startswith("data/indexes/"):
        raise ValueError("Adopted registry must be explicitly placed under data/indexes")
    target = Path(project).resolve() / target_relative
    source_sha = digest(source)
    exception_json = json.dumps(exceptions, sort_keys=True, allow_nan=False)
    if not isinstance(exceptions, list) or any(not isinstance(item, dict) for item in exceptions):
        raise ValueError("An explicit list of unresolved exceptions is required")
    if target.exists():
        meta = _registry_metadata(project, target, check_integrity=True)
        if (meta.get("storage_schema_version") != "1" or meta.get("adoption_source_sha256") != source_sha
                or meta.get("adoption_exceptions") != exception_json):
            raise ValueError("Conflicting registry adoption; destination already owned")
        return {"registry": target, "adopted": False, "exceptions": exceptions}
    with source.open("rb") as stream:
        header = stream.read(100)
    if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise ValueError("Adoption requires a committed non-WAL proposal")
    source_database = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    source_database.row_factory = sqlite3.Row
    staging = None
    try:
        meta = dict(source_database.execute("SELECT key,value FROM meta"))
        if (meta.get("schema_version") != "1" or meta.get("project") != str(Path(project).resolve())
            or meta.get("policy") != INSTANCE_REGISTRATION_POLICY):
            raise ValueError("Proposal project/schema mismatch")
        if source_database.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
            raise ValueError("Proposal integrity failure")
        for key in ("instance_id", "registration_key", "legacy_json"):
            if source_database.execute(f"SELECT 1 FROM instances GROUP BY {key} HAVING count(*)>1 LIMIT 1").fetchone():
                raise ValueError("Colliding proposal bindings")
        if source_database.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Invalid proposal foreign-key bindings")
        high_water = dict(source_database.execute("SELECT shot_id,high_water FROM shots"))
        for shot, number in high_water.items():
            parse_shot_id(shot)
            if type(number) is not int or number < 0:
                raise ValueError("Invalid shot ordinal reservation")
        seen_exceptions = set()
        for exception in exceptions:
            reference = exception.get("reference")
            if not isinstance(reference, str) or reference in seen_exceptions or exception.get("reason") != "missing field":
                raise ValueError("Unresolved records require unique exact references and missing-field reasons")
            if relative_path(project, reference) != reference:
                raise ValueError("Exception reference must be project-relative")
            payload = json.loads(locations.project_file(project, reference).read_text())
            if payload.get("field") not in (None, ""):
                raise ValueError("Exception evidence is not an unresolved missing field")
            if source_database.execute("SELECT 1 FROM instances WHERE legacy_json=?", (reference,)).fetchone():
                raise ValueError("Unresolved exception already has an allocated binding")
            seen_exceptions.add(reference)
        count = 0
        for row in source_database.execute("SELECT * FROM instances"):
            shot, number = parse_instance_id(row["instance_id"])
            if (shot != row["shot_id"] or number != row["instance_number"]
                    or parse_shot_id(shot)[0] != row["media_id"] or number > high_water.get(shot, -1)
                    or not re.fullmatch(r"[0-9a-f]{64}", row["registration_key"])
                    or row["registration_state"] not in ("registered", "retired")):
                raise ValueError("Incomplete/inconsistent proposed identity")
            locations.normalize_media_type(row["media_type"])
            if row["registration_state"] == "registered":
                record = json.loads(locations.project_file(project, row["legacy_json"]).read_text())
                locations.component(record.get("field"))
                if not isinstance(record.get("label"), str) or not record["label"].strip():
                    raise ValueError("Incomplete proposal classification")
                if record.get("media_id") != row["media_id"] or record.get("shot_id") != shot:
                    raise ValueError("Stale proposed source association")
                locations.project_file(project, row["legacy_png"])
            count += 1
        if interrupt:
            interrupt("validated")
        create_storage_directory(project, target.parent)
        relative_path(project, target.parent)
        descriptor, temporary = tempfile.mkstemp(prefix="." + target.name + "-adopting-", dir=target.parent)
        os.close(descriptor)
        staging = Path(temporary)
        with sqlite3.connect(staging) as output:
            source_database.backup(output)
            output.execute("PRAGMA foreign_keys=ON")
            _install_schema(output)
            for exception in exceptions:
                if not exception.get("reference") or not exception.get("reason"):
                    raise ValueError("Unresolved records require exact references and reasons")
                output.execute("INSERT INTO adoption_exceptions VALUES(?,?,?)", (exception["reference"], exception["reason"], json.dumps(exception)))
            output.executemany("INSERT INTO meta VALUES(?,?)", [("adoption_source_sha256", source_sha), ("adoption_exceptions", exception_json)])
            if output.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Invalid proposal foreign-key bindings")
            output.commit()
        if interrupt:
            interrupt("copied")
        if digest(source) != source_sha:
            raise ValueError("Proposal changed during adoption")
        with staging.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(staging, target)
        sync_directory(target.parent)
        staging.unlink()
        sync_directory(target.parent)
        return {"registry": target, "adopted": True, "instances": count, "exceptions": exceptions}
    finally:
        source_database.close()
        if staging is not None and staging.exists():
            staging.unlink()


class ResourceStore:
    """Explicit writer for an already adopted registry; never allocates IDs."""

    def __init__(self, project, registry, *, approved=False):
        if not approved:
            raise ValueError("Explicit storage write approval required")
        self.project = Path(project).resolve(strict=True)
        self.registry = locations.project_file(project, registry)
        if _registry_metadata(project, self.registry).get("storage_schema_version") != "1":
            raise ValueError("Registry must be explicitly adopted before publication")

    @contextmanager
    def transaction(self):
        with registry_lock(self.project, self.registry, exclusive=True):
            database = sqlite3.connect(self.registry.as_uri() + "?mode=rw", uri=True, timeout=30)
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA foreign_keys=ON")
            database.execute("PRAGMA synchronous=FULL")
            try:
                yield database
                database.commit()
            except BaseException:
                database.rollback()
                raise
            finally:
                database.close()

    def reserve_generation_run(self, instance_id, field, label, provider, mode):
        """Select and reserve the next ordinal in the existing run table, never recycle."""
        from uuid import uuid4
        with self.transaction() as database:
            if "reservation" not in {row["name"] for row in database.execute("PRAGMA table_info(generation_runs)")}:
                raise ValueError("Next-run reservations require an explicitly upgraded registry schema; reads never upgrade storage")
            binding = locations.registered_instance(self.project, self.registry, instance_id)
            record = locations.find_media(self.project, binding["media_type"], media_id=binding["media_id"])
            if mode not in locations.MODES:
                raise ValueError("Unsupported generation mode")
            number = database.execute("SELECT COALESCE(MAX(number),0)+1 FROM generation_runs WHERE instance_id=? AND provider=?",
                                      (instance_id, provider)).fetchone()[0]
            path = locations.generation_run_destination(self.project, record, binding["media_type"], instance_id, field, label, provider, number) / "run.json"
            token = uuid4().hex
            database.execute("INSERT INTO generation_runs(instance_id,provider,number,mode,state,run_path,run_sha256,manifest,stage,reservation) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (instance_id, provider, number, mode, "reserved", relative_path(self.project, path), "", "{}", "", token))
            return {"instance_id": instance_id, "provider": provider, "number": number, "mode": mode, "token": token}

    def finish_generation_attempt(self, reservation, state):
        if state not in ("failed", "abandoned"):
            raise ValueError("Only failed/abandoned attempts use this lifecycle operation")
        with self.transaction() as database:
            row = database.execute("SELECT state,reservation FROM generation_runs WHERE instance_id=? AND provider=? AND number=?",
                                   (reservation["instance_id"], reservation["provider"], reservation["number"])).fetchone()
            if row is None or row["reservation"] != reservation["token"] or row["state"] not in ("reserved", state):
                raise ValueError("Attempt ownership/state mismatch")
            database.execute("UPDATE generation_runs SET state=? WHERE instance_id=? AND provider=? AND number=?",
                             (state, reservation["instance_id"], reservation["provider"], reservation["number"]))

    def commit_location(self, database, binding, kind, mode, paths, metadata, expected_revision, *, operation_id):
        """Internal CAS commit; publication service owns pair validation/installation."""
        record_path = relative_path(self.project, paths["metadata"])
        image_path = relative_path(self.project, paths["image"])
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected revision must be a nonnegative integer")
        operation = database.execute("SELECT * FROM publication_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if (operation is None or operation["state"] != "files_published" or operation["instance_id"] != binding["instance_id"]
                or operation["kind"] != kind or operation["mode"] != mode):
            raise ValueError("Location commit requires its verified publication operation")
        details = json.loads(operation["details"])
        if (details["record_path"] != record_path or details["image_path"] != image_path or details["metadata"] != metadata
                or details["expected_revision"] != expected_revision or digest(paths["metadata"]) != details["record_sha256"]
                or digest(paths["image"]) != details["image_sha256"]):
            raise ValueError("Location commit differs from installed publication")
        previous = database.execute("SELECT revision FROM resource_locations WHERE instance_id=? AND kind=? AND mode=?",
                                    (binding["instance_id"], kind, mode)).fetchone()
        if (previous[0] if previous else 0) != expected_revision:
            raise ValueError("Location revision changed; explicit replacement review required")
        statement = (
            "INSERT INTO resource_locations VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(instance_id,kind,mode) DO UPDATE SET revision=excluded.revision, "
            "record_path=excluded.record_path,image_path=excluded.image_path, "
            "field=excluded.field,label=excluded.label, "
            "record_sha256=excluded.record_sha256,image_sha256=excluded.image_sha256"
        )
        values = (binding["instance_id"], kind, mode, "media-first-v1", expected_revision + 1,
                  record_path, image_path, metadata["field"], metadata["label"],
                  details["record_sha256"], details["image_sha256"])
        database.execute(statement, values)
        return expected_revision + 1

    def commit_annotation_update(self, database, operation_id):
        """Commit only the installed collection named by its publication intent."""
        operation = database.execute("SELECT * FROM publication_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if operation is None or operation["kind"] != "annotation-collection" or operation["state"] != "files_published":
            raise ValueError("Collection commit requires its files_published operation")
        details = json.loads(operation["details"])
        current = database.execute("SELECT * FROM annotation_layouts WHERE media_type=? AND media_id=?",
                                   (details["media_type"], details["media_id"])).fetchone()
        if (current is None or current["revision"] != details["expected_revision"]
                or current["layout"] != details["layout"] or current["filename"] != details["filename"]
                or json.loads(current["artifacts"]) != details["before"]):
            raise ValueError("Concurrent collection revision/hash conflict")
        for suffix, sha in details["artifacts"].items():
            path = locations.annotation_path(self.project, details["filename"], details["media_type"], suffix, layout=details["layout"])
            if digest(locations.project_file(self.project, path)) != sha:
                raise ValueError("Collection commit differs from installed publication")
        updated = database.execute("UPDATE annotation_layouts SET revision=revision+1,artifacts=? WHERE media_type=? AND media_id=? AND revision=?",
                                   (json.dumps(details["artifacts"]), details["media_type"], details["media_id"], details["expected_revision"]))
        if updated.rowcount != 1:
            raise ValueError("Concurrent collection revision conflict")
        return details["expected_revision"] + 1

    def commit_annotation_layout(self, record, media_type, layout, *, expected_revision=0, required=(".annotations.json",)):
        """Explicit collection commit, no file moves, rewrites or regeneration."""
        if type(expected_revision) is not int or expected_revision < 0 or not required:
            raise ValueError("Annotation commitment requires an explicit nonnegative revision and required artifact set")
        with self.transaction() as database:
            authoritative = locations.find_media(self.project, media_type, media_id=record["media_id"])
            if record != authoritative:
                raise ValueError("Annotation metadata binding is stale")
            collection = locations.annotation_artifacts(self.project, record, media_type, layout=layout, required=required)
            if collection["missing_required"]:
                raise ValueError("Missing required annotation artifacts")
            from services.resource_validation import validate_annotation_classifications
            entries = json.loads(collection["expected"][".annotations.json"].read_text())
            if not isinstance(entries, list):
                raise ValueError("Annotation commitment requires a shot list")
            for entry in entries:
                shot = entry.get("shot", {}) if isinstance(entry, dict) else {}
                annotation = shot.get("annotation", {}) if isinstance(shot, dict) else {}
                findings = validate_annotation_classifications(annotation)
                if findings:
                    raise ValueError("Annotation classification validation failed: " + json.dumps(findings))
            if set(collection["other"]) - set(collection["existing"]):
                raise ValueError("Incomplete target collection would omit existing annotation artifacts")
            for suffix in collection["conflicts"]:
                if digest(collection["expected"][suffix]) != digest(collection["other"][suffix]):
                    raise ValueError("Conflicting annotation collections")
            previous = database.execute("SELECT revision,artifacts FROM annotation_layouts WHERE media_type=? AND media_id=?",
                                        (media_type, record["media_id"])).fetchone()
            if (previous[0] if previous else 0) != expected_revision:
                raise ValueError("Annotation layout revision changed")
            artifacts = {suffix: digest(path) for suffix, path in collection["existing"].items()}
            if previous is not None:
                raise ValueError("Registered collection edits require controlled artifact publication, not layout adoption")
            database.execute("""INSERT INTO annotation_layouts VALUES(?,?,?,?,?,?)
              ON CONFLICT(media_type,media_id) DO UPDATE SET filename=excluded.filename,
              layout=excluded.layout,revision=excluded.revision,artifacts=excluded.artifacts""",
                             (media_type, record["media_id"], record["filename"], layout, expected_revision + 1, json.dumps(artifacts)))
            return {"layout": layout, "revision": expected_revision + 1, "artifacts": artifacts}