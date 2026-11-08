"""Read-only identity/location contract for media-first resources.

Builders never inspect or create destinations. Exact resolution requires an
explicit registry snapshot; it never allocates identities or searches indexes.
Legacy paths are confined to resource_locations_legacy and are transitional.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from data.media_id import label_slug, parse_instance_id, parse_shot_id
from data.metadata import normalize_media_type

ANNOTATION_SUFFIXES = (
    ".annotations.json", ".annotations.manifest.json", ".annotations.npy",
    ".annotations.txt", ".frames.manifest.json", ".frames.npy", ".frames.valid.npy", ".log",
)
MODES = ("frame", "isolated")
PROVIDERS = ("local", "openai")


def component(value):
    """Validate a literal filesystem component without renaming it."""
    if (not isinstance(value, str) or not value or value in (".", "..")
            or any(character in value for character in '/\\\x00<>:"|?*')
            or any(ord(character) < 32 for character in value)
            or value.endswith((".", " ")) or len(value.encode("utf-8")) > 255):
        raise ValueError("Missing or unsafe resource path component")
    return value


def media_item(record, media_type):
    """Movie: exact filename stem. Gameplay: existing authoritative game_ ID."""
    normalize_media_type(media_type)
    component(record.get("media_id"))
    filename = component(record.get("filename"))
    if media_type == "gameplay":
        if not record["media_id"].startswith("game_"):
            raise ValueError("Gameplay requires its existing game_ media ID")
        return record["media_id"]
    return component(Path(filename).stem)


def find_media(project, media_type, *, media_id=None, filename=None):
    """Exact metadata lookup, not title search; reject missing/ambiguous records."""
    from data.metadata import load_json_metadata
    normalize_media_type(media_type)
    if media_id is None and filename is None:
        raise ValueError("Exact media ID or filename required")
    matches = [record for record in load_json_metadata(project, media_type)
               if (media_id is None or record.get("media_id") == media_id)
               and (filename is None or record.get("filename") == filename)]
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous media metadata")
    media_item(matches[0], media_type)
    return matches[0]


def scene_annotation_root(project):
    """Preserve the existing scenes root; captions are stored in shotlist CSV."""
    return Path(project) / "data/annotations/scenes"


def annotation_destination(project, record, media_type, suffix=".annotations.json"):
    """Pure filename-preserving shot artifact destination."""
    if suffix not in ANNOTATION_SUFFIXES:
        raise ValueError("Unsupported annotation artifact/scope")
    item = media_item(record, media_type)
    root = Path(project) / "data/annotations/shots" / media_type / item
    return root / component(Path(record["filename"]).stem + suffix)


def annotation_collections(project, media_type, *, registry=None):
    """Enumerate exact collections without publishing or selecting transitional copies."""
    from data.metadata import load_json_metadata
    from data.resource_locations_legacy import annotation_filenames
    records = load_json_metadata(project, media_type)
    by_stem = {}
    for record in records:
        if record.get("filename"):
            stem = Path(record["filename"]).stem
            if stem in by_stem:
                raise ValueError("Ambiguous annotation media filename")
            by_stem[stem] = record
    if registry is None:
        for stem in annotation_filenames(project, media_type):
            by_stem.setdefault(stem, {"filename": stem + ".mp4"})
    for stem in sorted(by_stem):
        record = by_stem[stem]
        collection = annotation_artifacts(project, record, media_type, registry=registry, required=())
        if registry is not None and collection.get("changed"):
            raise ValueError("Committed annotation collection changed or missing")
        if collection["existing"]:
            yield record, collection


def annotation_root(project):
    return Path(project) / "data/annotations/shots"


def annotation_directory(project, media_type):
    normalize_media_type(media_type)
    return annotation_root(project) / media_type


def annotation_files(project, media_type, suffix=".annotations.json", *, registry=None):
    for record, collection in annotation_collections(project, media_type, registry=registry):
        if suffix in collection["existing"]:
            path = collection["existing"][suffix]
            if registry is not None:
                path = resolve_annotation(project, record["filename"], media_type, suffix, registry=registry)
            yield path


def artifact_directory(project, kind, record, media_type, instance_id, field, label):
    """Pure approved destination; classification cannot allocate/change identity."""
    if kind not in ("silhouettes", "engravings"):
        raise ValueError("Unsupported resource kind")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("Missing resource classification label")
    shot_id, _ = parse_instance_id(instance_id)
    if parse_shot_id(shot_id)[0] != record.get("media_id"):
        raise ValueError("Instance/media identity mismatch")
    return (Path(project) / "data" / kind / media_type / media_item(record, media_type)
            / component(field) / component(label_slug(label)) / component(instance_id))


def silhouette_destination(project, record, media_type, instance_id, field, label):
    base = artifact_directory(project, "silhouettes", record, media_type, instance_id, field, label)
    return {"dir": base, "metadata": base / "silhouette.json", "image": base / "silhouette.png"}


def engraving_destination(project, record, media_type, instance_id, field, label, mode):
    if mode not in MODES:
        raise ValueError("Unsupported engraving mode")
    base = artifact_directory(project, "engravings", record, media_type, instance_id, field, label)
    return {"dir": base, "metadata": base / f"engraving-{mode}.json", "image": base / f"engraving-{mode}.png"}


def generation_run_destination(project, record, media_type, instance_id, field, label, provider, number):
    """Format an explicitly reserved run number, never reserve or create one."""
    if provider not in PROVIDERS or type(number) is not int or number < 1:
        raise ValueError("Unsupported provider or invalid reserved run number")
    return artifact_directory(project, "engravings", record, media_type, instance_id, field, label) / "runs" / provider / f"run-{number:03d}"


def project_file(project, path):
    """Confine an existing regular file; reject symlinks including ancestors."""
    root = Path(project).resolve(strict=True)
    path = Path(path)
    path = path if path.is_absolute() else root / path
    relative = path.relative_to(root)
    if ".." in relative.parts:
        raise ValueError("Resource path escapes project")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symlink resource path is not authorized")
    if not current.is_file():
        raise FileNotFoundError("Missing/stale registered resource: " + str(current))
    return current


def registered_instance(project, registry, instance_id):
    """Read immutable v1 binding and optional v1 current-location rows.

    The registry must be explicit, inside the project, and use SQLite rollback
    journaling. WAL snapshots are rejected before connection to avoid sidecars.
    No schema creation, registration, repair or index rebuilding is performed.
    """
    parse_instance_id(instance_id)
    path = project_file(project, registry)
    with path.open("rb") as stream:
        header = stream.read(100)
    if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise ValueError("Registry requires a committed rollback-journal SQLite snapshot")
    database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    try:
        database.execute("PRAGMA query_only=ON")
        meta = dict(database.execute("SELECT key,value FROM meta"))
        if meta.get("schema_version") != "1" or meta.get("project") != str(Path(project).resolve()):
            raise ValueError("Registry version/project mismatch")
        rows = database.execute("SELECT * FROM instances WHERE instance_id=?", (instance_id,)).fetchall()
        if len(rows) != 1:
            raise ValueError("Missing or ambiguous registered instance")
        binding = dict(rows[0])
        if (binding.get("registration_state") != "registered"
                or binding["shot_id"] != parse_instance_id(instance_id)[0]
            or binding["instance_number"] != parse_instance_id(instance_id)[1]
                or binding["media_id"] != parse_shot_id(binding["shot_id"])[0]):
            raise ValueError("Retired or inconsistent registered instance")
        locations = []
        if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resource_locations'").fetchone():
            if meta.get("location_schema_version") != "1":
                raise ValueError("Unknown current-location schema")
            locations = [dict(row) for row in database.execute(
                "SELECT * FROM resource_locations WHERE instance_id=?", (instance_id,))]
        binding["locations"] = locations
        if database.execute("SELECT 1 FROM sqlite_master WHERE name='generation_runs'").fetchone():
            binding["runs"] = [dict(row) for row in database.execute("SELECT * FROM generation_runs WHERE instance_id=?", (instance_id,))]
        return binding
    finally:
        database.close()


def current_location(project, binding, kind, mode):
    rows = [row for row in binding["locations"] if row["kind"] == kind and row["mode"] == mode]
    if len(rows) > 1:
        raise ValueError("Ambiguous current resource location")
    if not rows:
        if binding["locations"]:
            raise FileNotFoundError("No current location registered for resource/mode")
        return None
    row = rows[0]
    if row.get("layout") != "media-first-v1" or type(row.get("revision")) is not int or row["revision"] < 1:
        raise ValueError("Unsupported or stale location revision")
    record = find_media(project, binding["media_type"], media_id=binding["media_id"])
    builder = silhouette_destination if kind == "silhouettes" else engraving_destination
    args = (project, record, binding["media_type"], binding["instance_id"], row["field"], row["label"])
    expected = builder(*args) if kind == "silhouettes" else builder(*args, mode)
    metadata_path = project_file(project, row["record_path"])
    image_path = project_file(project, row["image_path"])
    if metadata_path != expected["metadata"].absolute() or image_path != expected["image"].absolute():
        raise ValueError("Registered location differs from approved destination")
    from data.resource_storage import digest
    for key, path in (("record_sha256", metadata_path), ("image_sha256", image_path)):
        if key in row and digest(path) != row[key]:
            raise ValueError("Committed resource checksum differs from registry")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    identity_key = "instance_id" if kind == "silhouettes" else "source_instance_id"
    if (not isinstance(metadata, dict) or metadata.get(identity_key) != binding["instance_id"]
            or metadata.get("field") != row["field"] or metadata.get("label") != row["label"]
            or (kind == "engravings" and metadata.get("mode") != mode)):
        raise ValueError("Stale resource identity/classification/location binding")
    return {"instance_id": binding["instance_id"], "metadata": metadata_path,
            "image": image_path, "record": metadata, "layout": row["layout"], "revision": row["revision"]}


def annotation_path(project, filename, media_type, suffix=".annotations.json", *, layout=None, registry=None):
    """Existing callers default to legacy writes; new-layout reads are explicit."""
    from data.resource_locations_legacy import annotation_path as legacy_path
    committed = committed_annotation_layout(project, filename, media_type, registry) if registry is not None else None
    if committed is not None and layout is not None and committed != layout:
        raise ValueError("Requested annotation layout differs from committed authority")
    layout = committed or layout or "legacy-v1"
    if layout == "legacy-v1":
        return legacy_path(project, filename, media_type, suffix)
    if layout != "media-first-v1":
        raise ValueError("Unsupported annotation layout")
    return annotation_destination(project, find_media(project, media_type, filename=filename), media_type, suffix)


def resolve_annotation(project, filename, media_type, suffix=".annotations.json", *, layout=None, registry=None, required=True):
    """Exact read-only lookup; an explicit layout avoids treating staging as committed."""
    if registry is not None:
        from data.resource_storage import registry_lock, digest
        with registry_lock(project, registry):
            commitment = annotation_commitment(project, filename, media_type, registry)
            committed = commitment["layout"] if commitment else None
            if committed is not None and layout is not None and committed != layout:
                raise ValueError("Requested annotation layout differs from committed authority")
            path = annotation_path(project, filename, media_type, suffix, layout=committed or layout)
            if commitment is not None:
                if suffix not in commitment["artifacts"]:
                    raise ValueError("Annotation artifact has not been committed")
                path = project_file(project, path)
                if digest(path) != commitment["artifacts"][suffix]:
                    raise ValueError("Annotation artifact checksum differs from committed authority")
                if suffix in (".annotations.npy", ".annotations.txt", ".frames.npy", ".frames.valid.npy"):
                    with sqlite3.connect(project_file(project, registry).as_uri() + "?mode=ro", uri=True) as database:
                        rows = database.execute("SELECT details FROM publication_operations WHERE kind='annotation-collection' AND state IN ('committed','finished')").fetchall()
                    source_revision = artifact_revision = 0
                    for operation in rows:
                        details = json.loads(operation[0])
                        if details["filename"] != filename or details["media_type"] != media_type:
                            continue
                        revision = details["expected_revision"] + 1
                        if ".annotations.json" in details["changed"]:
                            source_revision = max(source_revision, revision)
                        if suffix in details["proposed"]:
                            artifact_revision = max(artifact_revision, revision)
                    if source_revision > artifact_revision:
                        raise ValueError("Stale derived annotation artifact; explicit rebuild required")
                return path
            return resolve_annotation(project, filename, media_type, suffix, layout=layout, required=required)
    path = annotation_path(project, filename, media_type, suffix, layout=layout, registry=registry)
    committed = committed_annotation_layout(project, filename, media_type, registry) if registry is not None else None
    if layout == "media-first-v1" and committed is None:
        legacy = annotation_path(project, filename, media_type, suffix)
        if legacy.exists():
            raise ValueError("Ambiguous transitional annotation copies; migration publication required")
    return path if not required and not path.exists() else project_file(project, path)


def committed_annotation_layout(project, filename, media_type, registry):
    """Read an explicitly committed layout; absence preserves legacy behavior."""
    commitment = annotation_commitment(project, filename, media_type, registry)
    return commitment["layout"] if commitment else None


def annotation_context_token(project, registry=None, media_type=None):
    """Cheap cache identity for explicit committed layouts; never inspect corpus files."""
    if registry is None:
        return None
    import hashlib
    from data.resource_storage import registry_lock, relative_path
    path = project_file(project, registry)
    with registry_lock(project, path), sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        if dict(database.execute("SELECT key,value FROM meta")).get("project") != str(Path(project).resolve()):
            raise ValueError("Annotation registry project mismatch")
        rows = []
        if database.execute("SELECT 1 FROM sqlite_master WHERE name='annotation_layouts'").fetchone():
            rows = database.execute("SELECT media_type,media_id,filename,layout,revision,artifacts FROM annotation_layouts WHERE (? IS NULL OR media_type=?) ORDER BY media_type,media_id", (media_type, media_type)).fetchall()
    return hashlib.sha256(json.dumps([relative_path(project, path), rows], sort_keys=True).encode()).hexdigest()


def annotation_commitment(project, filename, media_type, registry):
    """Read one registry commitment, not an inference from directory existence."""
    from data.resource_storage import registry_lock
    record = find_media(project, media_type, filename=filename)
    path = project_file(project, registry)
    with registry_lock(project, path), sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        meta = dict(database.execute("SELECT key,value FROM meta"))
        if meta.get("project") != str(Path(project).resolve()):
            raise ValueError("Annotation registry project mismatch")
        if not database.execute("SELECT 1 FROM sqlite_master WHERE name='annotation_layouts'").fetchone():
            return None
        row = database.execute("SELECT layout,filename,revision,artifacts FROM annotation_layouts WHERE media_type=? AND media_id=?",
                               (media_type, record["media_id"])).fetchone()
        if row is not None and row[1] != filename:
            raise ValueError("Stale committed annotation media filename")
        if row is None:
            return None
        if row[0] not in ("legacy-v1", "media-first-v1") or type(row[2]) is not int or row[2] < 1:
            raise ValueError("Unknown or invalid committed annotation layout")
        artifacts = json.loads(row[3])
        if not isinstance(artifacts, dict) or any(suffix not in ANNOTATION_SUFFIXES for suffix in artifacts):
            raise ValueError("Invalid committed annotation artifact manifest")
        return {"layout": row[0], "filename": row[1], "revision": row[2], "artifacts": artifacts}


def annotation_artifacts(project, record, media_type, *, layout=None, registry=None, required=(".annotations.json",)):
    """Enumerate only the exact approved artifact set; never glob or merge layouts."""
    from data.resource_storage import registry_lock, relative_path, digest
    if registry is not None:
        with registry_lock(project, registry):
            commitment = annotation_commitment(project, record["filename"], media_type, registry)
            committed = commitment["layout"] if commitment else None
            if committed is not None and layout is not None and committed != layout:
                raise ValueError("Requested collection differs from committed layout")
            collection = annotation_artifacts(project, record, media_type, layout=committed or layout, required=required)
            collection["commitment"] = commitment
            collection["changed"] = ([suffix for suffix, sha in commitment["artifacts"].items()
                                      if suffix not in collection["existing"] or digest(collection["existing"][suffix]) != sha]
                                     if commitment else [])
            return collection
    if any(suffix not in ANNOTATION_SUFFIXES for suffix in required):
        raise ValueError("Unknown required annotation suffix")
    committed = committed_annotation_layout(project, record["filename"], media_type, registry) if registry is not None else None
    if committed is not None and layout is not None and committed != layout:
        raise ValueError("Requested collection differs from committed layout")
    layout = committed or layout or "legacy-v1"
    expected = {suffix: annotation_path(project, record["filename"], media_type, suffix, layout=layout) for suffix in ANNOTATION_SUFFIXES}
    other_layout = "legacy-v1" if layout == "media-first-v1" else "media-first-v1"
    other = ({suffix: annotation_path(project, record["filename"], media_type, suffix, layout=other_layout) for suffix in ANNOTATION_SUFFIXES}
             if record.get("media_id") or other_layout == "legacy-v1" else {})
    for path in (*expected.values(), *other.values()):
        relative_path(project, path)
    existing = {suffix: project_file(project, path) for suffix, path in expected.items() if path.exists()}
    other_existing = {suffix: project_file(project, path) for suffix, path in other.items() if path.exists()}
    return {"layout": layout, "expected": expected, "existing": existing, "other": other_existing,
            "missing_required": [suffix for suffix in required if suffix not in existing],
            "optional": [suffix for suffix in ANNOTATION_SUFFIXES if suffix not in required],
            "conflicts": sorted(set(existing) & set(other_existing))}


def source_validation(instance_id, current_shot_id, *, registered_frame=None, current_frame=None):
    """Provisional dirty assessment only; never persist/reallocate/delete."""
    registered_shot, _ = parse_instance_id(instance_id)
    parse_shot_id(current_shot_id)
    reasons = []
    if registered_shot != current_shot_id:
        reasons.append("shot-definition-changed")
    if registered_frame != current_frame:
        reasons.append("source-frame-changed")
    return {"dirty": bool(reasons), "reasons": reasons, "instance_id": instance_id,
            "registered_shot_id": registered_shot, "current_shot_id": current_shot_id}