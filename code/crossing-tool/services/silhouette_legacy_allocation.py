"""Transitional legacy ordinal reservations; no production initialization command."""

import fcntl
import json
import re
import os
import sqlite3
import hashlib
import shutil
from contextlib import contextmanager
from pathlib import Path


MARKER = ".legacy-ordinal-high-water.json"
REGISTRY = ".legacy-ordinal-reservations.sqlite3"


def registry_path(project_path):
    return Path(project_path) / "data/silhouettes" / REGISTRY


def _namespace(label_dir):
    label_dir = Path(label_dir).resolve()
    for parent in label_dir.parents:
        if parent.name == "silhouettes" and parent.parent.name == "data":
            return parent.parent.parent, label_dir.relative_to(parent.parent.parent).as_posix()
    return None, None


def reserve_from_registry(label_dir, *, count=1):
    project, namespace = _namespace(label_dir)
    if project is None:
        return None
    path = registry_path(project)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("Unsafe ordinal registry")
    if type(count) is not int or count < 1:
        raise ValueError("Positive reservation count required")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=300) as database:
        database.execute("BEGIN IMMEDIATE")
        version = database.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version != ("1",):
            raise ValueError("Unsupported ordinal registry")
        row = database.execute("SELECT high_water FROM namespaces WHERE namespace=?", (namespace,)).fetchone()
        marker_max = _read_marker(label_dir) if (Path(label_dir) / MARKER).exists() else 0
        maximum = max(row[0] if row else 0, marker_max, working_maximum(label_dir))
        database.execute("INSERT INTO namespaces(namespace,high_water,seed_maximum,records) VALUES(?,?,?,0) ON CONFLICT(namespace) DO UPDATE SET high_water=excluded.high_water", (namespace, maximum + count, maximum))
    return maximum + 1


def prepare_registry(project_path, census_path, temporary_path):
    """Prepare complete legacy namespace maxima without allocating or publishing."""
    project = Path(project_path).resolve()
    temporary = Path(temporary_path)
    if os.path.lexists(temporary):
        raise FileExistsError("Prepared registry already exists")
    if temporary.is_relative_to(project / "data"):
        raise ValueError("Prepare outside production data")
    with sqlite3.connect(temporary, uri=True) as database:
        database.execute("PRAGMA temp_store_directory='" + str(temporary.parent).replace("'", "''") + "'")
        database.execute("ATTACH DATABASE ? AS census", (Path(census_path).resolve().as_uri() + "?mode=ro",))
        database.executescript("""
        CREATE TABLE namespaces(namespace TEXT PRIMARY KEY,high_water INTEGER NOT NULL CHECK(high_water>=0),seed_maximum INTEGER NOT NULL,records INTEGER NOT NULL);
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO namespaces SELECT substr(metadata_path,1,length(metadata_path)-length(object_id)-6),
            max(CAST(substr(object_id,8) AS INTEGER)),max(CAST(substr(object_id,8) AS INTEGER)),count(*)
            FROM census.records GROUP BY 1;
        INSERT INTO meta VALUES('schema_version','1');
        INSERT INTO meta VALUES('validated','false');
        """)
        database.execute("INSERT INTO meta VALUES('project',?)", (str(project),))
    verification = validate_prepared_registry(project, census_path, temporary)
    with sqlite3.connect(temporary) as database:
        database.execute("UPDATE meta SET value='true' WHERE key='validated'")
        database.execute("INSERT INTO meta VALUES('verification',?)", (json.dumps(verification, sort_keys=True),))
    return verification


def validate_prepared_registry(project_path, census_path, temporary_path):
    project = Path(project_path).resolve()
    connection = sqlite3.connect(Path(temporary_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("ATTACH DATABASE ? AS census", (Path(census_path).resolve().as_uri() + "?mode=ro",))
        records = connection.execute("SELECT count(*) FROM census.records").fetchone()[0]
        invalid = connection.execute("SELECT count(*) FROM census.records WHERE object_id IS NULL OR object_id NOT GLOB 'object_[0-9][0-9][0-9][0-9]' OR metadata_path NOT LIKE 'data/silhouettes/catalog/%'").fetchone()[0]
        if invalid:
            raise ValueError("Noncanonical corpus identities cannot seed legacy reservations")
        if connection.execute("SELECT sum(records) FROM namespaces").fetchone()[0] != records:
            raise ValueError("Registry does not cover the complete corpus")
        namespaces = 0
        checked = 0
        expected_namespaces = set()
        for namespace, maximum, seed, count in connection.execute("SELECT namespace,high_water,seed_maximum,records FROM namespaces ORDER BY namespace"):
            directory = project / namespace
            expected_namespaces.add(namespace)
            if not namespace.startswith("data/silhouettes/catalog/") or ".." in Path(namespace).parts or directory.is_symlink():
                raise ValueError("Unsafe registry namespace")
            ids = {"json": set(), "png": set()}
            for path in directory.iterdir():
                match = re.fullmatch(r"object_(\d+)\.(json|png)", path.name)
                if match:
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("Unsafe corpus asset")
                    ids[match.group(2)].add(int(match.group(1)))
            if len(ids["json"]) != count or ids["json"] != ids["png"] or maximum < max(ids["json"], default=0) or seed != maximum:
                raise ValueError("Namespace coverage or historical maximum mismatch")
            expected = connection.execute("SELECT max(CAST(substr(object_id,8) AS INTEGER)),count(*) FROM census.records WHERE metadata_path>=? AND metadata_path<?", (namespace + "/", namespace + "0")).fetchone()
            if expected != (seed, count):
                raise ValueError("Namespace disagrees with complete census")
            namespaces += 1
            checked += count * 2
        observed_namespaces = set()
        for media in ("movie", "gameplay"):
            base = project / "data/silhouettes/catalog" / media
            if not base.exists():
                continue
            for film in base.iterdir():
                if not film.is_dir():
                    continue
                for label in film.iterdir():
                    if label.is_dir() and any(re.fullmatch(r"object_\d+\.(json|png)", path.name) for path in label.iterdir()):
                        observed_namespaces.add(label.relative_to(project).as_posix())
        if observed_namespaces != expected_namespaces:
            raise ValueError("Uncensused or missing allocation namespaces")
        for relative, size, mtime, kind in connection.execute("SELECT path,size,mtime_ns,kind FROM census.files WHERE domain='data/silhouettes'"):
            path = project / relative
            info = path.lstat()
            if kind != "regular" or not path.is_file() or path.is_symlink() or (info.st_size, info.st_mtime_ns) != (size, mtime):
                raise ValueError("Corpus changed since verified census")
        return {"records": records, "namespaces": namespaces, "object_files_checked": checked, "all_states_reserved": True, "allocated_new_ids": 0, "verification": "Complete census namespace maxima and current JSON/PNG filename sets; every retained silhouette file size/mtime/kind checked. No image decoding or record rewriting."}
    finally:
        connection.close()


def publish_registry(project_path, census_path, temporary_path):
    """Publish one validated registry atomically; existing registries are refused."""
    project = Path(project_path).resolve()
    destination = registry_path(project)
    lock = project / "data/silhouettes/.catalog-extraction.lock"
    with lock.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(destination):
            raise FileExistsError("Production registry already exists")
        verification = validate_prepared_registry(project, census_path, temporary_path)
        with sqlite3.connect(Path(temporary_path).resolve().as_uri() + "?mode=ro", uri=True) as database:
            if database.execute("SELECT value FROM meta WHERE key='validated'").fetchone() != ("true",):
                raise ValueError("Registry has not been validated")
            if database.execute("SELECT value FROM meta WHERE key='project'").fetchone() != (str(project),):
                raise ValueError("Registry belongs to another project")
        digest = hashlib.sha256(Path(temporary_path).read_bytes()).hexdigest()
        pending = destination.with_suffix(".pending")
        if os.path.lexists(pending):
            raise FileExistsError("Pending registry already exists")
        try:
            shutil.copyfile(temporary_path, pending)
            with pending.open("rb") as registry:
                os.fsync(registry.fileno())
            os.replace(pending, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            pending.unlink(missing_ok=True)
    return {**verification, "registry_path": str(destination), "sha256": digest, "atomic_publication": True, "rollback": "With extraction stopped and no post-publication allocations, rename registry back to a retained report location. After allocations, preserve high_water maxima; never discard or reseed lower values."}


def registry_status(project_path, census_path):
    path = registry_path(Path(project_path).resolve())
    if not path.is_file() or path.is_symlink():
        return {"ready": False, "path": str(path), "reason": "Missing or unsafe registry"}
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as database:
            database.execute("ATTACH DATABASE ? AS census", (Path(census_path).resolve().as_uri() + "?mode=ro",))
            meta = dict(database.execute("SELECT key,value FROM meta"))
            missing = database.execute("""SELECT count(*) FROM census.records r LEFT JOIN namespaces n
                ON n.namespace=substr(r.metadata_path,1,length(r.metadata_path)-length(r.object_id)-6)
                WHERE n.namespace IS NULL OR n.high_water<CAST(substr(r.object_id,8) AS INTEGER)""").fetchone()[0]
            ready = meta.get("validated") == "true" and meta.get("schema_version") == "1" and meta.get("project") == str(Path(project_path).resolve()) and missing == 0
            namespaces, records = database.execute("SELECT count(*),sum(records) FROM namespaces").fetchone()
        return {"ready": ready, "path": str(path), "namespaces": namespaces, "seeded_records": records, "unreserved_existing_ids": missing, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "verification": json.loads(meta.get("verification", "{}"))}
    except (sqlite3.Error, ValueError) as exc:
        return {"ready": False, "path": str(path), "reason": str(exc)}


def working_maximum(label_dir):
    maximum = 0
    for path in Path(label_dir).glob("object_*.*"):
        match = re.fullmatch(r"object_(\d+)\.(png|json)", path.name)
        if match:
            maximum = max(maximum, int(match.group(1)))
    return maximum


@contextmanager
def _namespace_lock(label_dir):
    label_dir = Path(label_dir)
    if not label_dir.is_dir() or label_dir.is_symlink():
        raise ValueError("Existing non-symlink allocation namespace required")
    lock = label_dir / ".legacy-ordinal.lock"
    if lock.is_symlink():
        raise ValueError("Unsafe namespace lock")
    with lock.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _read_marker(label_dir):
    marker = Path(label_dir) / MARKER
    if marker.is_symlink():
        raise ValueError("Unsafe reservation marker")
    data = json.loads(marker.read_text())
    if data.get("schema_version") != 1 or type(data.get("high_water")) is not int or data["high_water"] < 0:
        raise ValueError("Malformed reservation marker; refusing allocation")
    return data["high_water"]


def _write_marker(label_dir, high_water):
    from data.annotate import atomic_write_text
    atomic_write_text(Path(label_dir) / MARKER, json.dumps({"schema_version": 1, "high_water": high_water}, sort_keys=True) + "\n")


def initialize_namespace(label_dir, historical_maximum):
    if type(historical_maximum) is not int or historical_maximum < 0:
        raise ValueError("Nonnegative historical maximum required")
    with _namespace_lock(label_dir):
        marker = Path(label_dir) / MARKER
        previous = _read_marker(label_dir) if marker.exists() else 0
        _write_marker(label_dir, max(previous, historical_maximum, working_maximum(label_dir)))


def reserve_legacy_ordinals(label_dir, *, count=1):
    if type(count) is not int or count < 1:
        raise ValueError("Positive reservation count required")
    with _namespace_lock(label_dir):
        maximum = max(_read_marker(label_dir), working_maximum(label_dir))
        _write_marker(label_dir, maximum + count)
        return maximum + 1


def require_reservations_after_archive(label_dir):
    for parent in Path(label_dir).parents:
        if parent.name == "silhouettes" and parent.parent.name == "data":
            if (parent.parent.parent / "archive/silhouettes").exists():
                raise ValueError("Archive exists but legacy namespace lacks reservations; approved initialization required")
            break