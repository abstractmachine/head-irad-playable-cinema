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
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    identity_key = "instance_id" if kind == "silhouettes" else "source_instance_id"
    if (metadata.get(identity_key) != binding["instance_id"]
            or metadata.get("field") != row["field"] or metadata.get("label") != row["label"]
            or (kind == "engravings" and metadata.get("mode") != mode)):
        raise ValueError("Stale resource identity/classification/location binding")
    return {"instance_id": binding["instance_id"], "metadata": metadata_path,
            "image": image_path, "record": metadata, "layout": row["layout"]}


def annotation_path(project, filename, media_type, suffix=".annotations.json", *, layout="legacy-v1"):
    """Existing callers default to legacy writes; new-layout reads are explicit."""
    from data.resource_locations_legacy import annotation_path as legacy_path
    if layout == "legacy-v1":
        return legacy_path(project, filename, media_type, suffix)
    if layout != "media-first-v1":
        raise ValueError("Unsupported annotation layout")
    return annotation_destination(project, find_media(project, media_type, filename=filename), media_type, suffix)


def resolve_annotation(project, filename, media_type, suffix=".annotations.json", *, layout="legacy-v1"):
    """Exact read-only lookup; an explicit layout avoids treating staging as committed."""
    path = annotation_path(project, filename, media_type, suffix, layout=layout)
    if layout == "media-first-v1":
        legacy = annotation_path(project, filename, media_type, suffix)
        if legacy.exists():
            raise ValueError("Ambiguous transitional annotation copies; migration publication required")
    return project_file(project, path)


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