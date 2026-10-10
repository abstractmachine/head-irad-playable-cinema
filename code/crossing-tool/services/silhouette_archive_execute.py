"""Journaled, no-overwrite archival against retained immutable plans."""

import ctypes
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path(project, relative):
    from services.silhouette_archive import archive_path
    original = relative.replace("archive/silhouettes/", "data/silhouettes/", 1)
    archive_path(original)
    if relative not in (original, archive_path(original)):
        raise ValueError("Invalid archival path")
    path = project / relative
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(project) and parent != project):
        raise ValueError("Symlinked archival path")
    return path


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir(path, project):
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(project)):
        raise ValueError("Unsafe directory ancestor")
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        _sync_directory(directory.parent)
    if path.is_symlink() or not path.resolve().is_relative_to(project):
        raise ValueError("Unsafe destination directory")


def _rename(source, destination):
    library = ctypes.CDLL(None, use_errno=True)
    function = library.renameat2
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def _verify(path, size, checksum, *, mtime=None):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
        raise ValueError(f"Unexpected file type, links or size: {path}")
    if mtime is not None and before.st_mtime_ns != mtime:
        raise ValueError(f"Changed source metadata: {path}")
    if digest_file(path) != checksum:
        raise ValueError(f"Content hash mismatch: {path}")
    after = path.lstat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError(f"File changed while hashing: {path}")


@contextmanager
def _lock(project):
    import fcntl
    with (project / "data/silhouettes/.catalog-extraction.lock").open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def plan_location(project, version):
    if not re.fullmatch(r"v\d+(?:-[a-z0-9-]+)?", version or ""):
        raise ValueError("Explicit valid plan version required")
    root = project / "outputs/tests/silhouette-archive" / version
    if any(parent.is_symlink() for parent in (root, *root.parents) if parent.is_relative_to(project) and parent != project):
        raise ValueError("Unsafe plan location")
    return root


def _plan_digest(root):
    paths = (root / 'plan.sqlite3', root / 'summary.json', root.parent / 'plan.sqlite3', root.parent / 'summary.json')
    return hashlib.sha256(''.join(digest_file(path) for path in paths).encode()).hexdigest()


def _connect(path):
    database = sqlite3.connect(path, timeout=300, uri=True)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=DELETE")
    database.execute("PRAGMA synchronous=FULL")
    return database


def journal_location(project, version):
    plan_location(project, version)
    return project / "outputs/tests/silhouette-identity-migration/archival-execution" / version


def initialize(project_path, version):
    from services.silhouette_archive import archive_path
    from services.silhouette_legacy_allocation import registry_status

    project = Path(project_path).resolve()
    plan = plan_location(project, version)
    summary = json.loads((plan / "summary.json").read_text())
    if summary.get("status") != "READY FOR ARCHIVAL REVIEW" or summary.get("remaining_blockers"):
        raise ValueError("Plan is not approved-stage ready")
    census = project / "outputs/tests/silhouette-identity-migration/census.sqlite3"
    if not registry_status(project, census)["ready"]:
        raise ValueError("Legacy registry incomplete")
    output = journal_location(project, version)
    if output.exists():
        raise FileExistsError("Execution journal already exists; use resume or status")
    _mkdir(output, project)
    with _lock(project), _connect(output / "journal.sqlite3") as database:
        database.execute("ATTACH DATABASE ? AS plan", ((plan / "plan.sqlite3").as_uri() + "?mode=ro",))
        database.executescript("""
        CREATE TABLE operation(operation_id TEXT,plan_digest TEXT,version TEXT,project TEXT,preflight INTEGER DEFAULT 0,created TEXT);
        CREATE TABLE files(source_path TEXT PRIMARY KEY,destination_path TEXT UNIQUE,expected_size INTEGER,expected_sha256 TEXT,mtime_ns INTEGER,metadata_path TEXT,state TEXT DEFAULT 'pending',attempt_count INTEGER DEFAULT 0,last_error TEXT,verification_state TEXT,completion_timestamp TEXT,pilot INTEGER DEFAULT 0);
        CREATE INDEX files_state ON files(state,source_path);
        CREATE TABLE removed_directories(path TEXT PRIMARY KEY,state TEXT);
        CREATE TABLE events(timestamp TEXT,event TEXT,detail TEXT);
        CREATE TABLE protected(path TEXT PRIMARY KEY,size INTEGER,mtime_ns INTEGER,sha256 TEXT);
        INSERT INTO files(source_path,destination_path,expected_size,expected_sha256,mtime_ns,metadata_path)
          SELECT path,destination,size,sha256,mtime_ns,CASE WHEN path LIKE '%.png' THEN substr(path,1,length(path)-4)||'.json' ELSE path END FROM plan.assets;
        """)
        database.execute("INSERT INTO operation(operation_id,plan_digest,version,project,created) VALUES(?,?,?,?,?)", (uuid.uuid4().hex, _plan_digest(plan), version, str(project), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        totals = database.execute("SELECT count(*),sum(expected_size),count(DISTINCT metadata_path) FROM files").fetchone()
        if tuple(totals) != (summary["unique_files"], summary["total_bytes"], summary["selected_records"]):
            raise ValueError("Plan population mismatch")
        for row in database.execute("SELECT * FROM files"):
            if archive_path(row["source_path"]) != row["destination_path"] or row["expected_size"] is None or not re.fullmatch(r"[0-9a-f]{64}", row["expected_sha256"] or ""):
                raise ValueError("Invalid plan asset contract")
            if Path(row["source_path"]).suffix not in (".json", ".png") or Path(row["source_path"]).stem != Path(row["metadata_path"]).stem:
                raise ValueError("Unexpected sidecar requires review")
        database.commit()
    _sync_directory(output)
    return output


def _open_operation(project, version):
    output = journal_location(project, version)
    path = output / "journal.sqlite3"
    if not path.is_file() or any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(project)):
        raise ValueError("Existing regular operation journal required")
    database = _connect(path)
    operation = database.execute("SELECT * FROM operation").fetchone()
    if operation["project"] != str(project) or operation["plan_digest"] != _plan_digest(plan_location(project, version)):
        database.close()
        raise ValueError("Immutable plan digest mismatch")
    return database


def preflight(project_path, version):
    from services.silhouette_catalog import assignment_state_for_record
    project = Path(project_path).resolve()
    with _lock(project), _open_operation(project, version) as database:
        if database.execute("SELECT count(*) FROM files WHERE state NOT IN ('pending','rolled_back')").fetchone()[0]:
            raise ValueError("Preflight requires an unmoved operation")
        database.execute("ATTACH DATABASE ? AS census", ((project / "outputs/tests/silhouette-identity-migration/census.sqlite3").as_uri() + "?mode=ro",))
        observed = 0
        for directory, folders, names in os.walk(project / "data/silhouettes", followlinks=False):
            if any((Path(directory) / folder).is_symlink() for folder in folders):
                raise ValueError("Symlink catalog directory")
            for name in names:
                path = Path(directory) / name
                relative = path.relative_to(project).as_posix()
                if name == '.legacy-ordinal-reservations.sqlite3':
                    continue
                if database.execute("SELECT 1 FROM census.files WHERE path=?", (relative,)).fetchone() is None:
                    raise ValueError(f"Unexpected uncensused file: {path}")
                observed += 1
        expected = database.execute("SELECT count(*) FROM census.files WHERE domain='data/silhouettes'").fetchone()[0]
        if observed != expected:
            raise ValueError("Current complete silhouette corpus differs from census")
        for number, row in enumerate(database.execute("SELECT * FROM files ORDER BY source_path"), 1):
            source = _path(project, row["source_path"])
            destination = _path(project, row["destination_path"])
            if os.path.lexists(destination):
                raise FileExistsError(f"Destination collision: {destination}")
            ancestor = destination.parent
            while not ancestor.exists():
                ancestor = ancestor.parent
            if source.stat().st_dev != ancestor.stat().st_dev:
                raise ValueError("Cross-filesystem archival is not supported")
            _verify(source, row["expected_size"], row["expected_sha256"], mtime=row["mtime_ns"])
            if source.suffix == ".json" and assignment_state_for_record(json.loads(source.read_text())) != "superseded":
                raise ValueError(f"Non-superseded planned record: {source}")
            if number % 100000 == 0:
                print(f"Preflight: {number:,} files freshly hashed", flush=True)
        database.execute("UPDATE operation SET preflight=1")
        database.execute("INSERT INTO events VALUES(?,?,?)", (_now(), "preflight", "All sources freshly SHA256 verified; lifecycle, size, mtime, type, destination and filesystem checked"))
    return status(project, version)


def protected_baseline(project_path, version, *, preferences_fingerprint=None):
    project = Path(project_path).resolve()
    with _lock(project), _open_operation(project, version) as database:
        if database.execute("SELECT 1 FROM events WHERE event='protected_baseline_complete'").fetchone():
            raise ValueError("Protected baseline already complete")
        database.execute("ATTACH DATABASE ? AS census", ((project / "outputs/tests/silhouette-identity-migration/census.sqlite3").as_uri() + "?mode=ro",))
        cursor = database.execute("SELECT c.path,c.size,c.mtime_ns,c.sha256 FROM census.files c WHERE NOT EXISTS(SELECT 1 FROM files f WHERE f.source_path=c.path) ORDER BY c.path")
        checked = 0
        for row in cursor:
            path = project / row[0]
            info = path.lstat()
            retained = database.execute('SELECT * FROM protected WHERE path=?', (row[0],)).fetchone()
            if retained:
                _verify(path, retained['size'], retained['sha256'], mtime=retained['mtime_ns'])
                checked += 1
                continue
            if row[0] == 'preferences/preferences.json' and preferences_fingerprint:
                _verify(path, preferences_fingerprint['size'], preferences_fingerprint['sha256'], mtime=preferences_fingerprint['mtime_ns'])
            elif not stat.S_ISREG(info.st_mode) or info.st_size != row[1]:
                raise ValueError(f"Protected baseline differs: {path}")
            digest = digest_file(path)
            if digest != row[3] and row[0] != 'preferences/preferences.json':
                raise ValueError(f"Protected census content differs: {path}")
            database.execute("INSERT INTO protected VALUES(?,?,?,?)", (row[0], info.st_size, info.st_mtime_ns, digest))
            checked += 1
            if checked % 100000 == 0:
                database.commit()
                print(f"Protected baseline: {checked:,} files freshly hashed", flush=True)
        registry = project / 'data/silhouettes/.legacy-ordinal-reservations.sqlite3'
        info = registry.stat()
        database.execute("INSERT OR IGNORE INTO protected VALUES(?,?,?,?)", (registry.relative_to(project).as_posix(), info.st_size, info.st_mtime_ns, digest_file(registry)))
        database.execute("INSERT INTO events VALUES(?,?,?)", (_now(), 'protected_baseline_complete', str(checked + 1)))
    return checked + 1


def verify_protected(project_path, version, *, allow_index_rebuild=False):
    from services.silhouette_archive import verify_namespace_counts
    project = Path(project_path).resolve()
    allowed = {f'data/indexes/illustration/{media}-silhouettes.sqlite3' for media in ('movie', 'gameplay')} if allow_index_rebuild else set()
    with _lock(project), _open_operation(project, version) as database:
        if not database.execute("SELECT 1 FROM events WHERE event='protected_baseline_complete'").fetchone():
            raise ValueError("Complete protected baseline required")
        checked = 0
        for row in database.execute("SELECT * FROM protected ORDER BY path"):
            if row['path'] in allowed:
                continue
            path = project / row['path']
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or (info.st_size, info.st_mtime_ns) != (row['size'], row['mtime_ns']) or digest_file(path) != row['sha256']:
                raise ValueError(f"Protected file changed: {path}")
            checked += 1
            if checked % 200000 == 0:
                print(f"Protected verification: {checked:,} fresh hashes matched", flush=True)
        database.execute("ATTACH DATABASE ? AS census", ((project / 'outputs/tests/silhouette-identity-migration/census.sqlite3').as_uri() + '?mode=ro',))
        namespace = verify_namespace_counts(project, database)
        moved = database.execute("SELECT count(*) FROM files WHERE state='verified'").fetchone()[0]
        silhouette_count = database.execute("SELECT count(*) FROM census.files WHERE domain='data/silhouettes'").fetchone()[0]
        expected_delta = {'data/silhouettes': {'census': silhouette_count, 'current': silhouette_count - moved}}
        if namespace['domain_count_differences'] != (expected_delta if moved else {}):
            raise ValueError(f"Unexpected protected namespace drift: {namespace}")
        database.execute("INSERT INTO events VALUES(?,?,?)", (_now(), 'protected_verified', json.dumps({'files': checked, 'allowed_indexes': sorted(allowed)})))
    return {'fresh_hashes_matched': checked, 'approved_index_exceptions': sorted(allowed), 'namespace_verification': namespace}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def select_pilot(project_path, version, count=32):
    project = Path(project_path).resolve()
    with _lock(project), _open_operation(project, version) as database:
        if database.execute("SELECT count(*) FROM files WHERE state!='pending'").fetchone()[0]:
            raise ValueError("Pilot must be selected before movement")
        database.execute("ATTACH DATABASE ? AS plan", ((project / "outputs/tests/silhouette-archive/plan.sqlite3").as_uri() + "?mode=ro",))
        selected = []
        for media in ("gameplay", "movie"):
            for replacement in (0, 1):
                query = "SELECT min(c.metadata_path) FROM plan.chosen c WHERE media=? AND (replacements NOT IN ('[]','{}','null'))=? AND EXISTS(SELECT 1 FROM files f WHERE f.source_path=c.metadata_path) GROUP BY label ORDER BY min(c.metadata_path) LIMIT ?"
                selected.extend(row[0] for row in database.execute(query, (media, replacement, max(2, count // 4))))
        if len(selected) < count:
            for row in database.execute("SELECT metadata_path FROM plan.chosen ORDER BY metadata_path"):
                if row[0] not in selected:
                    selected.append(row[0])
                if len(selected) >= count:
                    break
        selected = selected[:count]
        database.executemany("UPDATE files SET pilot=1 WHERE metadata_path=?", ((path,) for path in selected))
        database.execute("INSERT INTO events VALUES(?,?,?)", (_now(), "pilot_selected", json.dumps(selected)))
    return selected


def run_pilot(project_path, version):
    from services.silhouette_archive_reference import inspect_historical_lineage
    from data.annotate import atomic_write_text
    project = Path(project_path).resolve()
    output = journal_location(project, version)
    with _open_operation(project, version) as database:
        rows = list(database.execute('SELECT * FROM files WHERE pilot=1 ORDER BY source_path'))
        if not rows or any(row['state'] != 'pending' for row in rows):
            raise ValueError("Fresh selected pilot required; inspect or roll back existing pilot explicitly")
        records = sorted({row['metadata_path'] for row in rows})
    lookup = plan_location(project, version) / 'plan.sqlite3'
    evidence = {'pilot_records': records, 'pilot_files': len(rows), 'plan_version': version, 'gates': {}}
    atomic_write_text(output / 'pilot-manifest.json', json.dumps([dict(row) for row in rows], indent=2))
    def inspect():
        for reference in records:
            result = inspect_historical_lineage(project, reference, lookup=lookup)
            if any(edge.get('status') == 'error' for key in ('predecessors','replacements') for edge in result.get(key, [])):
                raise ValueError(f"Pilot historical lineage unresolved: {reference}")
        return len(records)
    try:
        inspect()
        evidence['first_apply'] = execute(project, version, pilot=True)
        execute(project, version, pilot=True, verify_only=True)
        evidence['gates']['historical_after_move'] = inspect()
        evidence['gates']['protected_after_move'] = verify_protected(project, version)
        evidence['rollback'] = execute(project, version, pilot=True, rollback=True)
        for row in rows:
            _verify(project / row['source_path'], row['expected_size'], row['expected_sha256'], mtime=row['mtime_ns'])
            if os.path.lexists(project / row['destination_path']):
                raise ValueError('Pilot rollback left destination')
        evidence['gates']['original_hashes_and_metadata_restored'] = len(rows)
        evidence['gates']['historical_after_rollback'] = inspect()
        evidence['gates']['protected_after_rollback'] = verify_protected(project, version)
        evidence['repeat_apply'] = execute(project, version, pilot=True)
        execute(project, version, pilot=True, verify_only=True)
        evidence['gates']['historical_after_repeat'] = inspect()
        evidence['gates']['protected_after_repeat'] = verify_protected(project, version)
        evidence['status'] = 'PASS'
        atomic_write_text(output / 'pilot-verification.json', json.dumps(evidence, indent=2))
        with _open_operation(project, version) as database:
            database.execute('INSERT INTO events VALUES(?,?,?)', (_now(), 'pilot_passed', json.dumps({'records': len(records), 'files': len(rows)})))
        return evidence
    except BaseException as exc:
        evidence.update(status='FAILED_STOP', error=str(exc))
        atomic_write_text(output / 'pilot-verification.json', json.dumps(evidence, indent=2))
        raise


def execute(project_path, version, *, pilot=False, rollback=False, verify_only=False, fault=None, batch_size=128):
    from services.silhouette_catalog import assignment_state_for_record
    project = Path(project_path).resolve()
    started = time.monotonic()
    with _lock(project), _open_operation(project, version) as database:
        if not database.execute("SELECT preflight FROM operation").fetchone()[0]:
            raise ValueError("Fresh complete preflight required")
        if not database.execute("SELECT 1 FROM events WHERE event='protected_baseline_complete'").fetchone():
            raise ValueError("Complete protected baseline required before movement")
        if not pilot and not rollback and not verify_only and not database.execute("SELECT 1 FROM events WHERE event='pilot_passed'").fetchone():
            raise ValueError("Mandatory successful pilot required before full archival")
        where = "pilot=1" if pilot else "1=1"
        if not verify_only:
            where += " AND state!='rolled_back'" if rollback else " AND state!='verified'"
        cursor = database.execute(f"SELECT * FROM files WHERE {where} ORDER BY metadata_path,source_path")
        completed = 0
        while rows := cursor.fetchmany(batch_size):
            directories = set()
            try:
                if not verify_only:
                    state = "rollback_pending" if rollback else "prepared"
                    database.executemany("UPDATE files SET state=?,attempt_count=attempt_count+1,last_error=NULL,verification_state=NULL WHERE source_path=?", ((state, row["source_path"]) for row in rows))
                    database.commit()
                for row in rows:
                    source = _path(project, row["source_path"])
                    destination = _path(project, row["destination_path"])
                    present_source = os.path.lexists(source)
                    present_destination = os.path.lexists(destination)
                    if present_destination and row['state'] in ('pending', 'rolled_back') and not rollback:
                        raise FileExistsError(f"Unjournaled destination collision: {destination}")
                    if present_source == present_destination:
                        raise ValueError(f"Ambiguous/missing filesystem state: {source}")
                    current = source if present_source else destination
                    _verify(current, row["expected_size"], row["expected_sha256"], mtime=row["mtime_ns"])
                    if not rollback:
                        metadata = _path(project, row["metadata_path"])
                        if not metadata.exists():
                            from services.silhouette_archive import archive_path
                            metadata = _path(project, archive_path(row["metadata_path"]))
                        if assignment_state_for_record(json.loads(metadata.read_text())) != "superseded":
                            raise ValueError("Non-superseded lifecycle at movement")
                    target = source if rollback else destination
                    if verify_only and present_source:
                        raise ValueError("Planned source remains during archive verification")
                    if not verify_only:
                        directories.update((source.parent, destination.parent))
                    if not verify_only and current != target:
                        _mkdir(target.parent, project)
                        if current.stat().st_dev != target.parent.stat().st_dev:
                            raise ValueError("Cross-filesystem move rejected")
                        with current.open("rb") as handle:
                            os.fsync(handle.fileno())
                        if fault:
                            fault("before_rename", row)
                        _rename(current, target)
                        directories.update((current.parent, target.parent))
                        if fault:
                            fault("after_rename", row)
                    _verify(target, row["expected_size"], row["expected_sha256"], mtime=row["mtime_ns"])
                    if fault:
                        fault("after_verify", row)
                for directory in directories:
                    _sync_directory(directory)
                final_state = "rolled_back" if rollback else "verified"
                if fault:
                    fault("before_commit", rows[-1])
                database.executemany("UPDATE files SET state=?,verification_state='fresh_sha256',completion_timestamp=?,last_error=NULL WHERE source_path=?", ((final_state, _now(), row["source_path"]) for row in rows))
                database.commit()
                completed += len(rows)
                if completed % (batch_size * 200) == 0:
                    elapsed = time.monotonic() - started
                    totals = status(project, version)
                    rate = completed/max(elapsed, .001)
                    print(f"Archive {'rollback' if rollback else 'verify' if verify_only else 'move'}: {totals['verified_files']//2:,} records; {totals['verified_files']:,} verified files; {totals['verified_bytes']:,} bytes; {totals['remaining_files']:,} remaining; errors={totals['errors']}; elapsed={elapsed:.1f}s; ETA={totals['remaining_files']/max(rate,.001):.1f}s", flush=True)
            except Exception as exc:
                for directory in directories:
                    _sync_directory(directory)
                database.execute("UPDATE files SET last_error=? WHERE source_path=?", (str(exc), row["source_path"]))
                database.execute("INSERT INTO events VALUES(?,?,?)", (_now(), "error", str(exc)))
                database.commit()
                raise
        database.execute("INSERT INTO events VALUES(?,?,?)", (_now(), "rollback" if rollback else "verify" if verify_only else "execute", f"pilot={pilot}; processed={completed}"))
        if rollback:
            for directory, in database.execute("SELECT path FROM removed_directories"):
                if (project / directory).is_dir():
                    database.execute("UPDATE removed_directories SET state='restored' WHERE path=?", (directory,))
    return status(project, version)


def verify_population(project_path, version):
    from services.silhouette_catalog import iter_catalog, assignment_state_for_record
    from services.silhouette_archive_reference import original_reference_path
    from collections import Counter
    project = Path(project_path).resolve()
    with _lock(project), _open_operation(project, version) as database:
        if database.execute("SELECT count(*) FROM files WHERE state!='verified' OR verification_state!='fresh_sha256' OR last_error IS NOT NULL").fetchone()[0]:
            raise ValueError("Incomplete fresh archival verification")
        working = Counter()
        for media in ('movie', 'gameplay'):
            for record in iter_catalog(str(project), media_type=media, include_history=True):
                if 'error' in record:
                    raise ValueError("Unreadable retained record")
                working[assignment_state_for_record(record)] += 1
                png = Path(record['path']).parent / record['png']
                if not png.is_file() or png.is_symlink():
                    raise ValueError("Required active/inactive asset missing")
        archive_records = 0
        lineage = 0
        for row in database.execute("SELECT * FROM files WHERE source_path LIKE '%.json'"):
            path = _path(project, row['destination_path'])
            if os.path.lexists(project / row['source_path']):
                raise ValueError("Planned source remains")
            record = json.loads(path.read_text())
            if assignment_state_for_record(record) != 'superseded':
                raise ValueError("Non-superseded archive record")
            assignment = record.get('assignment') or {}
            for field in ('supersedes', 'superseded_by'):
                references = assignment.get(field) or []
                references = [references] if isinstance(references, dict) else references
                for reference in references:
                    original = original_reference_path(project, reference)
                    from services.silhouette_archive import archive_path
                    if not (project / original).is_file() and not (project / archive_path(original)).is_file():
                        raise ValueError(f"Unresolved historical lineage: {original}")
                    lineage += 1
            archive_records += 1
        archived_files = 0
        for directory, folders, names in os.walk(project / 'archive/silhouettes'):
            for name in names:
                relative = (Path(directory) / name).relative_to(project).as_posix()
                if database.execute("SELECT 1 FROM files WHERE destination_path=?", (relative,)).fetchone() is None:
                    raise ValueError(f"Unplanned archive file: {relative}")
                archived_files += 1
        expected = json.loads((plan_location(project, version) / 'summary.json').read_text())['census_population']
        if dict(working) != {key: value for key, value in expected.items() if key != 'superseded'} or archive_records != expected['superseded'] or archived_files != database.execute('SELECT count(*) FROM files').fetchone()[0]:
            raise ValueError(f"Population mismatch: {dict(working)}, {archive_records}, {archived_files}")
        result = {'working': dict(working), 'archive_superseded': archive_records, 'archive_files': archived_files, 'lineage_edges_resolved': lineage}
        database.execute("INSERT INTO events VALUES(?,?,?)", (_now(), 'population_verified', json.dumps(result)))
    return result


def status(project_path, version):
    project = Path(project_path).resolve()
    path = journal_location(project, version) / "journal.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        states = dict(database.execute("SELECT state,count(*) FROM files GROUP BY state"))
        records, files, size = database.execute("SELECT count(DISTINCT metadata_path),count(*),sum(expected_size) FROM files").fetchone()
        verified = database.execute("SELECT count(*),coalesce(sum(expected_size),0) FROM files WHERE state='verified'").fetchone()
        errors = database.execute("SELECT count(*) FROM files WHERE last_error IS NOT NULL").fetchone()[0]
    return {"records": records, "files": files, "bytes": size, "states": states, "verified_files": verified[0], "verified_bytes": verified[1], "remaining_files": files-verified[0], "errors": errors}


def rebuild_silhouette_indexes(project_path, version):
    from services.illustration_index import index_path, rebuild_index
    from data.annotate import atomic_write_text
    import shutil
    project = Path(project_path).resolve()
    output = journal_location(project, version)
    with _lock(project), _open_operation(project, version) as database:
        if not database.execute("SELECT 1 FROM events WHERE event='population_verified'").fetchone():
            raise ValueError('Physical population acceptance required before index transition')
        if database.execute("SELECT count(*) FROM files WHERE state!='verified' OR last_error IS NOT NULL").fetchone()[0]:
            raise ValueError('Complete clean archival required before index transition')
        backups = output / 'index-backups'
        _mkdir(backups, project)
        evidence = {'status': 'in_progress', 'indexes': {}}
        for media in ('movie', 'gameplay'):
            path = index_path(project, 'silhouettes', media)
            baseline = database.execute('SELECT * FROM protected WHERE path=?', (path.relative_to(project).as_posix(),)).fetchone()
            if baseline is None:
                raise ValueError('Protected original index baseline required')
            _verify(path, baseline['size'], baseline['sha256'], mtime=baseline['mtime_ns'])
            backup = backups / path.name
            if backup.exists():
                raise FileExistsError('Index backup already exists; inspect retained transition evidence')
            shutil.copy2(path, backup)
            with backup.open('rb') as handle:
                os.fsync(handle.fileno())
            _verify(backup, baseline['size'], baseline['sha256'])
            evidence['indexes'][media] = {'old_path': path.relative_to(project).as_posix(), 'backup_path': backup.relative_to(project).as_posix(), 'old_size': baseline['size'], 'old_sha256': baseline['sha256']}
        _sync_directory(backups)
        atomic_write_text(output / 'index-transition.json', json.dumps(evidence, indent=2))
        expected = {}
        database.execute("ATTACH DATABASE ? AS census", ((project / 'outputs/tests/silhouette-identity-migration/census.sqlite3').as_uri() + '?mode=ro',))
        for media in ('movie', 'gameplay'):
            expected[media] = database.execute("SELECT count(*) FROM census.records WHERE media_type=? AND assignment='active'", (media,)).fetchone()[0]
            result = rebuild_index(project, 'silhouettes', media)
            if result['status'] != 'ready' or result['count'] != expected[media]:
                raise ValueError(f'Unexpected rebuilt index count: {result}')
            path = index_path(project, 'silhouettes', media)
            with path.open('rb') as handle:
                os.fsync(handle.fileno())
            _sync_directory(path.parent)
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as index:
                if index.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Rebuilt index integrity failure')
                states = dict(index.execute('SELECT assignment_state,count(*) FROM records GROUP BY assignment_state'))
                if states != ({'active': expected[media]} if expected[media] else {}):
                    raise ValueError('Non-active rebuilt index rows')
                for payload, in index.execute('SELECT payload FROM records'):
                    record = json.loads(payload)
                    relative = record['path']
                    if not relative.startswith(f'data/silhouettes/catalog/{media}/') or not (project / relative).is_file():
                        raise ValueError('Invalid active indexed record path')
                    png = (project / relative).parent / record['png']
                    if not png.is_file():
                        raise ValueError('Required active indexed image missing')
            evidence['indexes'][media].update(new_sha256=digest_file(path), new_size=path.stat().st_size, active_rows=expected[media], archived_rows=0, inactive_rows=0, integrity_check='ok', indexed_source_paths_verified=expected[media])
            atomic_write_text(output / 'index-transition.json', json.dumps(evidence, indent=2))
        evidence['status'] = 'PASS'
        atomic_write_text(output / 'index-transition.json', json.dumps(evidence, indent=2))
        database.execute('INSERT INTO events VALUES(?,?,?)', (_now(), 'indexes_rebuilt', json.dumps(expected)))
    return evidence


def prune_empty(project_path, version, *, fault=None):
    project = Path(project_path).resolve()
    with _lock(project), _open_operation(project, version) as database:
        if database.execute("SELECT count(*) FROM files WHERE state!='verified'").fetchone()[0]:
            raise ValueError("Pruning requires complete verified archival")
        if not database.execute("SELECT 1 FROM events WHERE event='population_verified'").fetchone():
            raise ValueError("Pruning requires accepted physical population")
        directories = set()
        for row in database.execute("SELECT source_path FROM files"):
            parent = _path(project, row[0]).parent
            directories.add(parent)
            directories.add(parent.parent)
        completed = 0
        for depth in sorted({len(path.parts) for path in directories}, reverse=True):
            level = sorted((path for path in directories if len(path.parts) == depth), key=str)
            for offset in range(0, len(level), 128):
                batch = [directory for directory in level[offset:offset + 128] if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir())]
                database.executemany("INSERT OR REPLACE INTO removed_directories VALUES(?,'prepared')", ((directory.relative_to(project).as_posix(),) for directory in batch))
                database.commit()
                changed_parents = set()
                try:
                    for directory in batch:
                        directory.rmdir()
                        changed_parents.add(directory.parent)
                        if fault:
                            fault('after_rmdir', directory)
                finally:
                    for parent in changed_parents:
                        _sync_directory(parent)
                database.executemany("UPDATE removed_directories SET state='removed' WHERE path=?", ((directory.relative_to(project).as_posix(),) for directory in batch))
                database.commit()
                completed += len(batch)
                if batch and completed % 12800 == 0:
                    print(f'Pruning: {completed:,} additional empty directories journaled', flush=True)
        for relative, in database.execute("SELECT path FROM removed_directories WHERE state='prepared'"):
            if not os.path.lexists(project / relative):
                database.execute("UPDATE removed_directories SET state='removed' WHERE path=?", (relative,))
        database.execute("INSERT INTO events VALUES(?,?,?)", (_now(), 'pruning_complete', str(database.execute("SELECT count(*) FROM removed_directories WHERE state='removed'").fetchone()[0])))
    return status(project, version)