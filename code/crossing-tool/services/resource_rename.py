"""Fixture-only file renames over the frozen media-first preparation schema.

No activation, content transformation, copying, cleanup or production entry point.
"""

from contextlib import closing, contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from data.resource_storage import digest, relative_path
from services.resource_migration import readonly
from services.maintenance_preflight import check_quiescence
from tool import project_access as access


_FIELDS = ("source", "destination", "size", "mtime_ns", "ctime_ns", "inode", "device", "links", "sha256", "instance_id", "mode", "owner")


def _event(interrupt, phase):
    if interrupt is not None:
        interrupt(phase)


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _parts(project, reference):
    if not isinstance(reference, str) or Path(reference).is_absolute():
        raise ValueError("Plan requires project-relative file paths")
    if not reference or any(part in ("", ".", "..") for part in reference.split("/")):
        raise ValueError("Noncanonical or escaping file path")
    relative_path(project, reference)
    if reference.split("/")[0] in (access.CONTROL, access.ANCHOR):
        raise ValueError("Plan cannot move maintenance evidence")
    return reference.split("/")


@contextmanager
def _parent(root, parts, *, create=False):
    descriptor = os.dup(root)
    try:
        for component in parts[:-1]:
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            if os.fstat(child).st_dev != os.fstat(root).st_dev:
                os.close(child)
                raise ValueError("Cross-device ancestry is forbidden")
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _directory_identity(descriptor):
    info = os.fstat(descriptor)
    return [info.st_dev, info.st_ino]


def _verify(parent, name, frozen, *, original=False):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != frozen["size"]
                or info.st_dev != frozen["device"] or info.st_ino != frozen["inode"]
                or info.st_mtime_ns != frozen["mtime_ns"]):
            raise ValueError("Unexpected file type, identity or size")
        if original and info.st_ctime_ns != frozen["ctime_ns"]:
            raise ValueError("Frozen source metadata changed")
        checksum = hashlib.sha256()
        while chunk := os.read(descriptor, 4 * 1024 * 1024):
            checksum.update(chunk)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink", "st_mode", "st_uid", "st_gid")
        if (checksum.hexdigest() != frozen["sha256"] or any(getattr(info, field) != getattr(after, field) for field in fields)
                or os.stat(name, dir_fd=parent, follow_symlinks=False).st_ino != info.st_ino):
            raise ValueError("File content changed or checksum mismatch")
        return True
    finally:
        os.close(descriptor)


def _rename(source_parent, source, destination_parent, destination):
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise OSError("renameat2 no-replace is unavailable") from error
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_parent, os.fsencode(source), destination_parent, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination)


def _capacity(root, count, path_bytes, directory_components):
    info = os.fstatvfs(root)
    reserve = max(1024 * 1024, count * 4096 + path_bytes * 4 + directory_components * max(4096, info.f_frsize))
    if info.f_bavail * info.f_frsize < reserve or info.f_favail < directory_components + 16:
        raise OSError(28, "Insufficient journal/directory headroom")
    return reserve


def _commit(database, parent, interrupt, phase):
    _event(interrupt, phase + ":before-journal-sync")
    database.execute("CREATE TABLE IF NOT EXISTS progress_clock(started REAL NOT NULL,updated REAL NOT NULL,phase TEXT NOT NULL)")
    now = time.time()
    if database.execute("SELECT 1 FROM progress_clock").fetchone() is None:
        database.execute("INSERT INTO progress_clock VALUES(?,?,?)", (now, now, phase))
    else:
        database.execute("UPDATE progress_clock SET updated=?,phase=?", (now, phase))
    database.commit()
    os.fsync(parent)
    _event(interrupt, phase + ":journal-synced")


def _record(database, parent, source, state, interrupt, phase, parents=None):
    database.execute("BEGIN IMMEDIATE")
    database.execute("UPDATE moves SET state=?,parents=coalesce(?,parents) WHERE source=?",
                     (state, _encode(parents) if parents is not None else None, source))
    database.execute("INSERT INTO events(source,state,recorded) VALUES(?,?,?)", (source, state, time.time()))
    _commit(database, parent, interrupt, phase)


def _plan(project, plan, approved_digest, plan_identity):
    if not re.fullmatch("[0-9a-f]{64}", approved_digest or ""):
        raise ValueError("Explicit approved SHA256 required")
    plan = Path(plan)
    reference = relative_path(project, plan)
    plan = project / reference
    info = plan.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Frozen plan must be a regular single-link file")
    if digest(plan) != approved_digest:
        raise ValueError("Frozen plan digest mismatch")
    return plan, reference, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _rows(plan):
    for row in plan.execute("SELECT " + ",".join(_FIELDS) + " FROM files WHERE disposition='Move' ORDER BY source"):
        yield dict(row)


def _validate_row(project, row, plan_reference):
    source, destination = (_parts(project, row[key]) for key in ("source", "destination"))
    if row["source"] == row["destination"] or plan_reference in (row["source"], row["destination"]):
        raise ValueError("Move aliases itself or its frozen plan")
    if row["links"] != 1 or not re.fullmatch("[0-9a-f]{64}", row["sha256"] or ""):
        raise ValueError("Missing single-link frozen checksum")
    if any(type(row[key]) is not int or row[key] < 0 for key in ("size", "inode", "device", "mtime_ns", "ctime_ns")):
        raise ValueError("Incomplete frozen filesystem identity")
    return source, destination


def _run_moves(owner, root, database, journal_parent, project, plan_reference, plan_stat, reverse, interrupt):
    order = "DESC" if reverse else "ASC"
    comparison = "<" if reverse else ">"
    previous = None
    while True:
        stored = database.execute("SELECT * FROM moves" + (" WHERE source" + comparison + "?" if previous is not None else "")
                                  + " ORDER BY source " + order + " LIMIT 1", (previous,) if previous is not None else ()).fetchone()
        if stored is None:
            break
        previous = stored["source"]
        owner.require_fixture_renames()
        info = (project / plan_reference).lstat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != plan_stat:
            raise ValueError("Frozen plan changed during execution")
        frozen = json.loads(stored["frozen"])
        source, destination = _validate_row(project, frozen, plan_reference)
        if frozen["device"] != os.fstat(root).st_dev:
            raise ValueError("Source is not on the project filesystem")
        state = stored["state"]
        if state not in ("planned", "intent", "done", "undo-intent", "undone"):
            raise ValueError("Unknown journal move state")
        evidence = database.execute("SELECT state FROM events WHERE source=? ORDER BY sequence DESC LIMIT 1", (stored["source"],)).fetchone()
        if (state == "planned" and (evidence is not None or stored["parents"] is not None)
                or state != "planned" and (evidence is None or evidence[0] != state or stored["parents"] is None)):
            raise ValueError("Incomplete journal intent/completion evidence")
        if not reverse and state in ("undo-intent", "undone"):
            raise ValueError("Rollback journal cannot be resumed forward")
        with _parent(root, source) as (source_parent, source_name), _parent(root, destination, create=True) as (destination_parent, destination_name):
            identities = [_directory_identity(source_parent), _directory_identity(destination_parent)]
            if stored["parents"] is not None and json.loads(stored["parents"]) != identities:
                raise ValueError("Recorded filesystem directory identity changed")
            source_exists = _verify(source_parent, source_name, frozen, original=state == "planned")
            destination_exists = _verify(destination_parent, destination_name, frozen)
            if source_exists == destination_exists:
                raise ValueError("Ambiguous move: both paths present or both absent")
            if state == "planned" and not source_exists or state == "done" and not reverse and not destination_exists:
                raise ValueError("Journal intent/completion contradicts filesystem")
            if state == "undone" and not source_exists:
                raise ValueError("Rollback completion contradicts filesystem")
            desired = source_exists if reverse else destination_exists
            finished = "undone" if reverse else "done"
            prefix = "rollback:" if reverse else ""
            if desired:
                _event(interrupt, prefix + "before-directory-sync")
                os.fsync(source_parent)
                os.fsync(destination_parent)
                if state != finished:
                    _record(database, journal_parent, stored["source"], finished, interrupt, prefix + "completion", identities)
                continue
            _event(interrupt, prefix + "before-intent")
            _record(database, journal_parent, stored["source"], "undo-intent" if reverse else "intent", interrupt, prefix + "intent", identities)
            _event(interrupt, prefix + "intent-committed")
            owner.require_fixture_renames()
            _event(interrupt, prefix + "before-rename")
            info = (project / plan_reference).lstat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != plan_stat:
                raise ValueError("Frozen plan changed during execution")
            for parts, identity in ((source, identities[0]), (destination, identities[1])):
                with _parent(root, parts) as (descriptor, _name):
                    if _directory_identity(descriptor) != identity:
                        raise ValueError("Path ancestry substituted before rename")
            if not _verify(destination_parent if reverse else source_parent, destination_name if reverse else source_name, frozen):
                raise ValueError("Source disappeared before rename")
            if reverse:
                _rename(destination_parent, destination_name, source_parent, source_name)
            else:
                _rename(source_parent, source_name, destination_parent, destination_name)
            _event(interrupt, prefix + "renamed")
            _event(interrupt, prefix + "before-directory-sync")
            os.fsync(source_parent)
            os.fsync(destination_parent)
            _event(interrupt, prefix + "directories-synced")
            _event(interrupt, prefix + "before-completion")
            owner.require_fixture_renames()
            for parts, identity in ((source, identities[0]), (destination, identities[1])):
                with _parent(root, parts) as (descriptor, _name):
                    if _directory_identity(descriptor) != identity:
                        raise ValueError("Path ancestry substituted after rename")
            if not _verify(source_parent if reverse else destination_parent, source_name if reverse else destination_name, frozen):
                raise ValueError("Renamed file is absent")
            _record(database, journal_parent, stored["source"], finished, interrupt, prefix + "completion")
            _event(interrupt, prefix + "completion-committed")
            _event(interrupt, prefix + "between-moves")


def execute(owner, plan, *, approved_digest, plan_identity, clients_stopped=False, known_pids=(), action="forward", interrupt=None):
    """Execute or reverse only Move rows in an allocated temporary project."""
    if not isinstance(owner, access._InspectionOwner):
        raise access.MaintenanceError("Existing exclusive inspection owner required")
    state = owner.require_fixture_renames()
    check_quiescence(owner, clients_stopped=clients_stopped, known_pids=known_pids)
    if action not in ("forward", "rollback"):
        raise ValueError("Unknown file-move action")
    project = Path(state["project"])
    plan_path, plan_reference, plan_stat = _plan(project, plan, approved_digest, plan_identity)
    with closing(readonly(plan_path)) as frozen:
        metadata = {row[0]: json.loads(row[1]) for row in frozen.execute("SELECT key,value FROM meta")}
        revision = metadata.get("plan_revision", {}).get("stage", metadata.get("stage"))
        if metadata.get("project") != str(project) or not plan_identity or revision != plan_identity:
            raise ValueError("Frozen plan project/revision identity mismatch")
        if frozen.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or frozen.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Frozen plan integrity failure")
        if frozen.execute("SELECT 1 FROM issues WHERE blocking<>0 LIMIT 1").fetchone():
            raise ValueError("Frozen plan has blocking issues")
        if frozen.execute("SELECT 1 FROM files a JOIN files b ON a.destination=b.source WHERE a.disposition='Move' LIMIT 1").fetchone():
            raise ValueError("Overlapping move graph requires a separately approved plan")
        count, path_bytes, directory_components = frozen.execute("SELECT count(*),coalesce(sum(length(CAST(source AS BLOB))+length(CAST(destination AS BLOB))),0),coalesce(sum(length(destination)-length(replace(destination,'/',''))),0) FROM files WHERE disposition='Move'").fetchone()
        if not count:
            raise ValueError("Frozen plan contains no moves")
        journal_name = "rename-" + state["operation"] + ".sqlite3"
        with access._root(project) as (_path, root), _parent(root, [access.CONTROL, journal_name]) as (parent, name):
            reserve = _capacity(root, count, path_bytes, directory_components)
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if action != "forward":
                    raise ValueError("Rollback requires the original durable journal")
                _event(interrupt, "before-journal-create")
                descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                info = os.fstat(descriptor)
                os.fsync(descriptor)
                os.close(descriptor)
                os.fsync(parent)
                fresh = True
            else:
                fresh = False
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Unsafe journal file")
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    sidecar = os.stat(name + suffix, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (not stat.S_ISREG(sidecar.st_mode) or sidecar.st_nlink != 1
                        or sidecar.st_uid != os.getuid() or sidecar.st_mode & 0o077):
                    raise ValueError("Unsafe SQLite recovery sidecar")
            header = {"version": 1, "project": str(project), "device": state["device"], "inode": state["inode"],
                      "token": state["token"], "operation": state["operation"], "plan": plan_reference,
                      "plan_identity": plan_identity, "plan_digest": approved_digest, "plan_stat": list(plan_stat),
                      "journal_inode": info.st_ino, "journal_device": info.st_dev, "moves": count}
            database = sqlite3.connect(f"file:/proc/self/fd/{parent}/{name}?mode=rw", uri=True)
            database.row_factory = sqlite3.Row
            try:
                database.execute("PRAGMA synchronous=FULL")
                database.execute("PRAGMA journal_mode=DELETE")
                if fresh:
                    database.executescript("CREATE TABLE meta(payload TEXT NOT NULL,direction TEXT NOT NULL); CREATE TABLE moves(source TEXT PRIMARY KEY,destination TEXT UNIQUE NOT NULL,frozen TEXT NOT NULL,state TEXT NOT NULL,parents TEXT); CREATE TABLE events(sequence INTEGER PRIMARY KEY,source TEXT,state TEXT NOT NULL,recorded REAL NOT NULL); CREATE INDEX events_source ON events(source,sequence);")
                    database.execute("INSERT INTO meta VALUES(?,'forward')", (_encode(header),))
                    for row in _rows(frozen):
                        _validate_row(project, row, plan_reference)
                        database.execute("INSERT INTO moves VALUES(?,?,?,'planned',NULL)", (row["source"], row["destination"], _encode(row)))
                    _commit(database, parent, interrupt, "prepared")
                if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Journal integrity failure")
                headers = database.execute("SELECT * FROM meta").fetchall()
                if len(headers) != 1 or json.loads(headers[0]["payload"]) != header or headers[0]["direction"] not in ("forward", "rollback"):
                    raise ValueError("Journal identity or plan binding mismatch")
                rows = database.execute("SELECT * FROM moves ORDER BY source")
                for actual, expected in zip(rows, _rows(frozen), strict=True):
                    if actual["source"] != expected["source"] or actual["destination"] != expected["destination"] or json.loads(actual["frozen"]) != expected:
                        raise ValueError("Journal mapping differs from frozen plan")
                if action == "forward" and headers[0]["direction"] != "forward":
                    raise ValueError("Rollback operation cannot resume forward")
                if action == "rollback" and headers[0]["direction"] == "forward":
                    database.execute("UPDATE meta SET direction='rollback'")
                    _commit(database, parent, interrupt, "rollback-start")
                check_quiescence(owner, clients_stopped=clients_stopped, known_pids=known_pids)
                _run_moves(owner, root, database, parent, project, plan_reference, plan_stat, action == "rollback", interrupt)
                final = "undone" if action == "rollback" else "done"
                if database.execute("SELECT count(*) FROM moves WHERE state!=?", (final,)).fetchone()[0]:
                    raise ValueError("File-move operation is incomplete")
                owner.require_fixture_renames()
                return {"state": "rolled_back" if action == "rollback" else "files_moved", "moves": count,
                        "journal": str(project / access.CONTROL / journal_name), "reserve_bytes": reserve,
                        "resource_mutation_authorized": False, "graph_accepted": False}
            finally:
                database.close()