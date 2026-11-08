"""Explicit recoverable publication of already generated files, never inference.

The registry owns operation state and current revisions. Readers share its lock;
interrupted installation fails closed until explicit idempotent recovery. Verified
before-images preserve replacements; no generic resource deletion is implemented.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
from uuid import uuid4

from data import resource_locations as locations
from data.resource_storage import ResourceStore, digest, relative_path, registry_lock, create_storage_directory
from services.resource_validation import validate_metadata, validate_generation


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy(source, target):
    shutil.copyfile(source, target)
    _sync(target)


def _validation(store, database, binding, metadata, kind, mode):
    row = database.execute("SELECT layout FROM annotation_layouts WHERE media_type=? AND media_id=?",
                           (binding["media_type"], binding["media_id"])).fetchone()
    binding["runs"] = [dict(item) for item in database.execute("SELECT * FROM generation_runs WHERE instance_id=?", (binding["instance_id"],))]
    return validate_metadata(store.project, binding, metadata, kind, mode, annotation_layout=row[0] if row else "legacy-v1")


def publish_silhouette(project, metadata, image, *, source_path=None, registry=None, expected_revision=0, approved=False):
    """One validation boundary, legacy production or explicitly registered publication."""
    from services.resource_validation import validate_source_classification
    from data.annotate import atomic_write_text
    if registry is not None:
        return publish_resource(project, registry, metadata["instance_id"], "silhouettes", metadata, image,
                                expected_revision=expected_revision, approved=approved)
    findings = validate_source_classification(project, metadata, segmentation=True)
    if findings["state"] != "valid":
        raise ValueError("Silhouette publication validation failed: " + json.dumps(findings))
    if source_path is None:
        raise ValueError("Legacy publication requires its explicitly allocated source path")
    path = Path(source_path)
    relative_path(project, path)
    from data.resource_locations_legacy import silhouette_catalog_item
    expected_dir = silhouette_catalog_item(project, metadata["media_type"], Path(metadata["filename"]).stem, metadata["label"])
    if path.parent != expected_dir or not path.name.startswith("object_") or path.suffix != ".json":
        raise ValueError("Legacy publication path differs from the existing catalog owner")
    previous = json.loads(path.read_text()) if path.exists() else {}
    if previous.get("source_validation", {}).get("dirty"):
        metadata = {**metadata, "source_validation": previous["source_validation"]}
    png_path = path.with_name(metadata["png"])
    relative_path(project, png_path)
    if png_path.parent != path.parent:
        raise ValueError("Legacy PNG must belong to its allocated object")
    if hasattr(image, "save"):
        image.save(str(png_path), format="PNG", optimize=False)
    elif Path(image) != png_path:
        _copy(locations.project_file(project, image), png_path)
    atomic_write_text(path, json.dumps(metadata, indent=2, ensure_ascii=False))
    return {"metadata": path, "image": png_path, "layout": "legacy-v1"}


def publish_engraving(project, source, metadata, image, mode, *, registry=None, approved=False, expected_revision=0):
    """Shared final publication boundary; keep legacy outputs until explicit cutover."""
    from data.annotate import atomic_write_text
    if registry is not None:
        return publish_resource(project, registry, metadata["source_instance_id"], "engravings", metadata, image,
                                mode=mode, approved=approved, expected_revision=expected_revision)
    from services.engraving_paths import engraving_paths
    from services.silhouette_catalog import require_active_record
    record = require_active_record(source["record"], source=source["metadata"], project_path=project)
    if mode not in locations.MODES or metadata.get("mode") != mode or metadata.get("status") != "generated":
        raise ValueError("Engraving publication mode/status mismatch")
    described = metadata.get("silhouette", {})
    for key in ("media_id", "shot_id", "field", "label"):
        if key in described and described[key] != record.get(key):
            raise ValueError("Engraving source relationship mismatch: " + key)
    if source.get("instance_id") is not None:
        metadata = {**metadata, "source_instance_id": source["instance_id"]}
    if record.get("source_validation", {}).get("dirty"):
        metadata = {**metadata, "source_validation": record["source_validation"]}
    paths = engraving_paths(project, source["metadata"], record, mode)
    previous = json.loads(paths["metadata"].read_text()) if paths["metadata"].exists() else {}
    if previous.get("source_validation", {}).get("dirty"):
        metadata = {**metadata, "source_validation": previous["source_validation"]}
    paths["dir"].mkdir(parents=True, exist_ok=True)
    if Path(image) != paths["engraving_png"]:
        _copy(Path(image), paths["engraving_png"])
    atomic_write_text(paths["metadata"], json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    return {"metadata": paths["metadata"], "image": paths["engraving_png"], "layout": "legacy-v1"}


def publish_generation_run(project, registry, instance_id, field, label, provider, number, payload, artifacts, *, approved=False, interrupt=None, reservation_token=None):
    """Publish one immutable completed run; explicit identical retry finishes interruption.

    The supplied positive run ordinal is reserved transactionally, never inferred
    during lookup. A complete same-filesystem directory is renamed before its run
    commitment. No run can become an Illustration and no existing run is replaced.
    """
    from data.annotate import atomic_write_text
    store = ResourceStore(project, registry, approved=approved)
    with store.transaction() as database:
        binding = locations.registered_instance(project, registry, instance_id)
        source = locations.current_location(project, binding, "silhouettes", "")
        if source is None or source["record"].get("field") != field or source["record"].get("label") != label:
            raise ValueError("Generation run requires the current canonical source classification")
        if (not isinstance(payload, dict) or payload.get("source_instance_id") != instance_id
                or payload.get("mode") not in locations.MODES or not isinstance(payload.get("generation"), dict)
                or not isinstance(payload.get("source"), dict) or payload["source"].get("image_sha256") != digest(source["image"])):
            raise ValueError("Invalid generation run source/mode/metadata")
        reproduction = validate_generation(payload["generation"], provider)
        if reproduction["missing"] or reproduction["errors"]:
            raise ValueError("Incomplete or contradictory generation run reproduction metadata")
        record = locations.find_media(project, binding["media_type"], media_id=binding["media_id"])
        target = locations.generation_run_destination(project, record, binding["media_type"], instance_id, field, label, provider, number)
        relative_path(project, target)
        artifact_sources = {}
        for name, path in artifacts.items():
            locations.component(name)
            if name in ("run.json", "engraving.json", "engraving-frame.json", "engraving-isolated.json", "silhouette.json"):
                raise ValueError("Generation artifacts cannot become resource publications")
            artifact_sources[name] = locations.project_file(project, path)
        manifest = {name: digest(path) for name, path in artifact_sources.items()}
        run_text = json.dumps({**payload, "artifacts": manifest}, sort_keys=True, allow_nan=False)
        run_sha = hashlib.sha256(run_text.encode()).hexdigest()
        previous = database.execute("SELECT * FROM generation_runs WHERE instance_id=? AND provider=? AND number=?", (instance_id, provider, number)).fetchone()
        run_relative = relative_path(project, target / "run.json")
        reserved = previous is not None and previous["state"] == "reserved"
        if reserved and (previous["reservation"] != reservation_token or previous["run_path"] != run_relative or previous["mode"] != payload["mode"]):
            raise ValueError("Generation reservation ownership mismatch")
        if previous is not None and not reserved:
            if previous["run_path"] != run_relative or previous["run_sha256"] != run_sha or json.loads(previous["manifest"]) != manifest:
                raise ValueError("Generation run ordinal already reserved by different content")
            stage = store.project / relative_path(project, previous["stage"])
            if stage.parent != store.registry.parent / ".resource-runs":
                raise ValueError("Run staging is outside its controlled owner")
        else:
            if target.exists():
                raise ValueError("Uncommitted generation run destination already exists")
            stage = store.registry.parent / ".resource-runs" / uuid4().hex
            create_storage_directory(project, stage, exclusive=True)
            for name, path in artifact_sources.items():
                _copy(path, stage / name)
            atomic_write_text(stage / "run.json", run_text)
            _sync(stage / "run.json")
            _sync(stage)
            if reserved:
                database.execute("UPDATE generation_runs SET state='prepared',run_sha256=?,manifest=?,stage=? WHERE instance_id=? AND provider=? AND number=?",
                                 (run_sha, json.dumps(manifest, sort_keys=True), relative_path(project, stage), instance_id, provider, number))
            else:
                database.execute("INSERT INTO generation_runs(instance_id,provider,number,mode,state,run_path,run_sha256,manifest,stage) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (instance_id, provider, number, payload["mode"], "prepared", run_relative, run_sha,
                                  json.dumps(manifest, sort_keys=True), relative_path(project, stage)))
            database.commit()
            if interrupt:
                interrupt("run-prepared")
        installed = target if target.exists() else stage
        if digest(locations.project_file(project, installed / "run.json")) != run_sha:
            raise ValueError("Prepared generation run checksum mismatch")
        for name, sha in manifest.items():
            if digest(locations.project_file(project, installed / name)) != sha:
                raise ValueError("Prepared generation artifact checksum mismatch")
        if previous is not None and previous["state"] == "complete":
            return {"run": target / "run.json", "sha256": run_sha, "published": False}
        if not target.exists():
            create_storage_directory(project, target.parent)
            os.rename(stage, target)
            _sync(target.parent)
            _sync(stage.parent)
        if interrupt:
            interrupt("run-installed")
        database.execute("UPDATE generation_runs SET state='complete' WHERE instance_id=? AND provider=? AND number=?", (instance_id, provider, number))
        database.commit()
        return {"run": target / "run.json", "sha256": run_sha, "published": True}


def _operation_paths(store, binding, operation_id, kind, mode, details):
    expected_stage = store.registry.parent / ".resource-publications" / operation_id
    if details["stage"] != relative_path(store.project, expected_stage):
        raise ValueError("Publication journal stage is outside its operation owner")
    record = locations.find_media(store.project, binding["media_type"], media_id=binding["media_id"])
    builder = locations.silhouette_destination if kind == "silhouettes" else locations.engraving_destination
    args = (store.project, record, binding["media_type"], binding["instance_id"], details["metadata"]["field"], details["metadata"]["label"])
    paths = builder(*args) if kind == "silhouettes" else builder(*args, mode)
    if (details["record_path"] != relative_path(store.project, paths["metadata"])
            or details["image_path"] != relative_path(store.project, paths["image"])):
        raise ValueError("Publication journal location differs from canonical ownership")
    return expected_stage, paths


def _cleanup(stage):
    if not stage.exists():
        return
    allowed = {"new.json", "new.png", "previous.json", "previous.png"}
    if any(path.name not in allowed or path.is_symlink() or not path.is_file() for path in stage.iterdir()):
        raise ValueError("Publication cleanup refuses unrelated files")
    shutil.rmtree(stage)
    _sync(stage.parent)


def _complete(store, database, binding, operation_id, kind, mode, stage):
    from services.illustration_index import invalidate_index
    binding["locations"] = [dict(row) for row in database.execute("SELECT * FROM resource_locations WHERE instance_id=?", (binding["instance_id"],))]
    locations.current_location(store.project, binding, kind, mode)
    invalidate_index(store.project, kind, binding["media_type"])
    database.execute("UPDATE publication_operations SET state='finished' WHERE operation_id=?", (operation_id,))
    database.commit()
    _cleanup(stage)


def publish_resource(project, registry, instance_id, kind, metadata, image, *, mode="", expected_revision=0,
                     approved=False, interrupt=None):
    """Publish an explicit pair with CAS revision. Interrupted operations need recovery."""
    from data.annotate import atomic_write_text
    from PIL import Image
    from services.resource_resolution import _silhouette
    store = ResourceStore(project, registry, approved=approved)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("Expected revision must be a nonnegative integer")
    binding = locations.registered_instance(project, registry, instance_id)
    operation_id = uuid4().hex
    with store.transaction() as database:
        pending = database.execute("SELECT 1 FROM publication_operations WHERE state NOT IN ('finished','rolled_back')").fetchone()
        if pending:
            raise ValueError("Pending publication requires explicit recovery")
        binding = locations.registered_instance(project, registry, instance_id)
        _silhouette(project, binding)
        validation = _validation(store, database, binding, metadata, kind, mode)
        if validation["state"] != "valid":
            raise ValueError("Publication metadata rejected: " + json.dumps({key: validation[key] for key in ("state", "missing", "errors")}))
        paths = validation["destination"]
        metadata_text = json.dumps(metadata, sort_keys=True, allow_nan=False)
        for path in paths.values():
            relative_path(project, path)
        image = locations.project_file(project, image)
        with Image.open(image) as picture:
            if picture.format != "PNG":
                raise ValueError("Publication image must be PNG")
            picture.verify()
        previous = database.execute("SELECT * FROM resource_locations WHERE instance_id=? AND kind=? AND mode=?", (instance_id, kind, mode)).fetchone()
        if (previous["revision"] if previous else 0) != expected_revision:
            raise ValueError("Location revision changed")
        if previous is not None:
            old = locations.current_location(project, binding, kind, mode)
            if old["record"].get("source_validation", {}).get("dirty") and not metadata.get("source_validation", {}).get("dirty"):
                raise ValueError("Publication cannot implicitly clear a prior dirty assessment")
        for key in ("metadata", "image"):
            if paths[key].exists() and (previous is None or paths[key] != store.project / previous["record_path" if key == "metadata" else "image_path"]):
                raise ValueError("Uncommitted/foreign destination already exists")
        stage = store.registry.parent / ".resource-publications" / operation_id
        relative_path(project, stage)
        create_storage_directory(project, stage, exclusive=True)
        atomic_write_text(stage / "new.json", metadata_text)
        _copy(image, stage / "new.png")
        _sync(stage / "new.json")
        before = {}
        for key, name in (("metadata", "previous.json"), ("image", "previous.png")):
            if paths[key].exists():
                before[key] = digest(paths[key])
                _copy(paths[key], stage / name)
        details = {"stage": relative_path(project, stage), "record_path": relative_path(project, paths["metadata"]),
                   "image_path": relative_path(project, paths["image"]), "record_sha256": digest(stage / "new.json"),
                   "image_sha256": digest(stage / "new.png"), "expected_revision": expected_revision,
                   "metadata": metadata, "before": before}
        _sync(stage)
        database.execute("INSERT INTO publication_operations VALUES(?,?,?,?,?,?)", (operation_id, instance_id, kind, mode, "prepared", json.dumps(details)))
        database.commit()
        if interrupt:
            interrupt("prepared")
        _finish(store, database, binding, operation_id, kind, mode, details, interrupt)
    return {"operation_id": operation_id, "revision": expected_revision + 1, "published": True}


def _finish(store, database, binding, operation_id, kind, mode, details, interrupt=None):
    from PIL import Image
    stage, paths = _operation_paths(store, binding, operation_id, kind, mode, details)
    previous = database.execute("SELECT revision FROM resource_locations WHERE instance_id=? AND kind=? AND mode=?",
                                (binding["instance_id"], kind, mode)).fetchone()
    if type(details["expected_revision"]) is not int or (previous[0] if previous else 0) != details["expected_revision"]:
        raise ValueError("Recovery location revision differs from the approved operation")
    if (digest(locations.project_file(store.project, stage / "new.json")) != details["record_sha256"]
            or digest(locations.project_file(store.project, stage / "new.png")) != details["image_sha256"]):
        raise ValueError("Prepared publication checksum mismatch")
    if json.loads((stage / "new.json").read_text()) != details["metadata"]:
        raise ValueError("Prepared JSON differs from approved metadata")
    with Image.open(stage / "new.png") as picture:
        if picture.format != "PNG":
            raise ValueError("Prepared image must be PNG")
        picture.verify()
    database.execute("UPDATE publication_operations SET state='installing' WHERE operation_id=?", (operation_id,))
    database.commit()
    create_storage_directory(store.project, paths["metadata"].parent)
    for key, name in (("image", "new.png"), ("metadata", "new.json")):
        temporary = paths[key].parent / (".publication-" + operation_id + "-" + name)
        relative_path(store.project, temporary)
        _copy(stage / name, temporary)
        os.replace(temporary, paths[key])
        _sync(paths[key].parent)
        if interrupt:
            interrupt(key + "-installed")
    database.execute("UPDATE publication_operations SET state='files_published' WHERE operation_id=?", (operation_id,))
    database.commit()
    if digest(paths["metadata"]) != details["record_sha256"] or digest(paths["image"]) != details["image_sha256"]:
        raise ValueError("Installed publication checksum mismatch")
    if interrupt:
        interrupt("before-commit")
    store.commit_location(database, binding, kind, mode, paths, details["metadata"], details["expected_revision"], operation_id=operation_id)
    database.execute("UPDATE publication_operations SET state='committed' WHERE operation_id=?", (operation_id,))
    database.commit()
    if interrupt:
        interrupt("committed")
    _complete(store, database, binding, operation_id, kind, mode, stage)


def recover_publication(project, registry, operation_id, *, approved=False, action="complete"):
    """Explicit idempotent completion or rollback; never delete a prior resource."""
    store = ResourceStore(project, registry, approved=approved)
    if action not in ("complete", "rollback"):
        raise ValueError("Unknown recovery action")
    with store.transaction() as database:
        row = database.execute("SELECT * FROM publication_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown publication operation")
        details = json.loads(row["details"])
        binding = dict(database.execute("SELECT * FROM instances WHERE instance_id=?", (row["instance_id"],)).fetchone())
        stage, paths = _operation_paths(store, binding, operation_id, row["kind"], row["mode"], details)
        if row["state"] in ("finished", "rolled_back"):
            _cleanup(stage)
            return {"operation_id": operation_id, "state": row["state"]}
        if action == "rollback":
            if row["state"] == "committed":
                raise ValueError("Committed publication cannot be implicitly rolled back")
            for key, backup in (("metadata", "previous.json"), ("image", "previous.png")):
                if key in details["before"] and digest(locations.project_file(project, stage / backup)) != details["before"][key]:
                    raise ValueError("Publication before-image corrupted")
            for key, backup in (("metadata", "previous.json"), ("image", "previous.png")):
                target = paths[key]
                if row["state"] == "prepared":
                    continue
                if key in details["before"]:
                    if digest(locations.project_file(project, stage / backup)) != details["before"][key]:
                        raise ValueError("Publication before-image corrupted")
                    temporary = target.parent / (".publication-" + operation_id + "-rollback-" + backup)
                    relative_path(project, temporary)
                    _copy(stage / backup, temporary)
                    os.replace(temporary, target)
                    _sync(target.parent)
                elif target.exists():
                    expected = details["record_sha256" if key == "metadata" else "image_sha256"]
                    if digest(target) != expected:
                        raise ValueError("Recovery refuses removal of unrelated content")
                    target.unlink()
                    _sync(target.parent)
            database.execute("UPDATE publication_operations SET state='rolled_back' WHERE operation_id=?", (operation_id,))
            database.commit()
            _cleanup(stage)
        elif row["state"] == "committed":
            _complete(store, database, binding, operation_id, row["kind"], row["mode"], stage)
        else:
            binding["locations"] = [dict(item) for item in database.execute("SELECT * FROM resource_locations WHERE instance_id=?", (row["instance_id"],))]
            validation = _validation(store, database, binding, details["metadata"], row["kind"], row["mode"])
            if validation["state"] != "valid":
                raise ValueError("Recovery requires valid source/publication metadata")
            _finish(store, database, binding, operation_id, row["kind"], row["mode"], details)
        return {"operation_id": operation_id, "state": "rolled_back" if action == "rollback" else "finished"}


def publication_status(project, registry, *, instance_id=None):
    """Operational read of pending/completed journals, including interrupted installs."""
    import sqlite3
    path = locations.project_file(project, registry)
    with registry_lock(project, registry, exclusive=True), sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute("SELECT * FROM publication_operations WHERE (? IS NULL OR instance_id=?) ORDER BY operation_id", (instance_id, instance_id))
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]