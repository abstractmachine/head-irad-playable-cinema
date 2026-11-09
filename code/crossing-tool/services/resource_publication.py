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
    return validate_metadata(store.project, binding, metadata, kind, mode, annotation_layout=row[0] if row else "legacy-v1",
                             preserve_assignment=kind == "silhouettes" and bool(binding["locations"]))


def publish_silhouette(project, metadata, image, *, source_path=None, registry=None, expected_revision=0, approved=False):
    """One validation boundary, legacy production or explicitly registered publication."""
    from services.resource_validation import validate_source_classification
    from data.annotate import atomic_write_text
    if registry is not None:
        if hasattr(image, "save"):
            import tempfile
            store = ResourceStore(project, registry, approved=approved)
            with tempfile.TemporaryDirectory(prefix=".silhouette-input-", dir=store.registry.parent) as temporary:
                prepared = Path(temporary) / "silhouette.png"
                image.save(prepared, format="PNG")
                return publish_resource(project, registry, metadata["instance_id"], "silhouettes", metadata, prepared,
                                        expected_revision=expected_revision, approved=approved)
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


def resume_registered_generation_run(project, registry, instance_id, provider, number, *, approved=False):
    store = ResourceStore(project, registry, approved=approved)
    with store.transaction() as database:
        row = database.execute("SELECT * FROM generation_runs WHERE instance_id=? AND provider=? AND number=?", (instance_id, provider, number)).fetchone()
        if row is None or row["state"] not in ("prepared", "complete"):
            raise ValueError("Only a retained prepared/completed run can resume without inference")
        target = store.project / relative_path(project, row["run_path"])
        staged = store.project / relative_path(project, row["stage"]) / "run.json"
        if staged.parent.parent != store.registry.parent / ".resource-runs":
            raise ValueError("Retained run staging is outside its controlled owner")
        source_path = locations.project_file(project, target if target.exists() else staged)
        payload = json.loads(source_path.read_text())
        manifest = payload.pop("artifacts")
        binding = locations.registered_instance(project, registry, instance_id)
        current = locations.current_location(project, binding, "silhouettes", "")
        return publish_generation_run(project, registry, instance_id, current["record"]["field"], current["record"]["label"], provider, number,
                                      payload, {name: source_path.parent / name for name in manifest}, approved=approved,
                                      reservation_token=row["reservation"])


def publish_registered_engraving_run(project, registry, instance_id, provider, number, *, approved=False, expected_revision=0, interrupt=None):
    store = ResourceStore(project, registry, approved=approved)
    with store.transaction() as database:
        row = database.execute("SELECT * FROM generation_runs WHERE instance_id=? AND provider=? AND number=?", (instance_id, provider, number)).fetchone()
        if row is not None and row["state"] == "prepared":
            resume_registered_generation_run(project, registry, instance_id, provider, number, approved=approved)
            row = database.execute("SELECT * FROM generation_runs WHERE instance_id=? AND provider=? AND number=?", (instance_id, provider, number)).fetchone()
        if row is None or row["state"] != "complete":
            raise ValueError("Explicit selection requires an immutable completed generation run")
        run_path = locations.project_file(project, row["run_path"])
        if digest(run_path) != row["run_sha256"]:
            raise ValueError("Selected run commitment changed")
        payload = json.loads(run_path.read_text())
        if payload.get("review_state") == "rejected":
            raise ValueError("Automatically rejected generation is not publishable")
        pending = database.execute("SELECT * FROM publication_operations WHERE kind NOT IN ('dirty-propagation','annotation-collection') AND state NOT IN ('finished','rolled_back')").fetchall()
        for operation in pending:
            details = json.loads(operation["details"])
            if (operation["instance_id"] != instance_id or operation["kind"] != "engravings" or operation["mode"] != row["mode"]
                    or details["metadata"].get("published_run") != {"provider": provider, "number": number, "sha256": row["run_sha256"]}):
                raise ValueError("Unrelated pending publication requires explicit recovery")
            recover_publication(project, registry, operation["operation_id"], approved=approved)
        from services.resource_resolution import resolve_silhouette
        source = resolve_silhouette(project, instance_id, registry=registry)
        binding = locations.registered_instance(project, registry, instance_id)
        current = locations.current_location(project, binding, "engravings", row["mode"]) if any(item["kind"] == "engravings" and item["mode"] == row["mode"] for item in binding["locations"]) else None
        metadata = {**(current["record"] if current else {}), **source["record"], "resource_kind": "engravings", "source_instance_id": instance_id,
                    "mode": row["mode"], "status": "generated", "generation": payload["generation"], "source": payload["source"],
                    "generation_service": payload["generation"]["service"], "review_state": payload.get("review_state", "needs_review"),
                    "published_run": {"provider": provider, "number": number, "sha256": row["run_sha256"]}}
        metadata.pop("instance_id", None)
        if current:
            for key in ("assignment", "human_best", "source_validation"):
                if key in current["record"]:
                    metadata[key] = current["record"][key]
            if current["record"].get("reviewed_at") and current["record"].get("published_run") != metadata["published_run"]:
                metadata["prior_review"] = {key: current["record"][key] for key in ("review_state", "reviewed_at", "published_run") if key in current["record"]}
                metadata.pop("reviewed_at", None)
        output = locations.project_file(project, run_path.parent / "result.png")
        binding = locations.registered_instance(project, registry, instance_id)
        findings = validate_metadata(project, binding, metadata, "engravings", row["mode"])
        if findings["state"] not in ("valid", "historical"):
            raise ValueError("Selected run publication rejected: " + json.dumps(findings))
        if current is not None and current["record"].get("published_run") == metadata["published_run"]:
            return {"published": False, "revision": current["revision"], "metadata": current["metadata"], "image": current["image"]}
        result = publish_resource(project, registry, instance_id, "engravings", metadata, output, mode=row["mode"],
                      expected_revision=expected_revision, approved=approved, interrupt=interrupt)
        binding = locations.registered_instance(project, registry, instance_id)
        installed = locations.current_location(project, binding, "engravings", row["mode"])
        return {**result, "metadata": installed["metadata"], "image": installed["image"]}


def complete_registered_engraving_run(project, registry, reservation, generation, source_info, artifacts, *, approved=False,
                                    publish_selected=False, expected_revision=0, review_state="needs_review", interrupt=None):
    from services.resource_resolution import resolve_silhouette
    source = resolve_silhouette(project, reservation["instance_id"], registry=registry)
    result = publish_generation_run(project, registry, reservation["instance_id"], source["record"]["field"], source["record"]["label"],
                                    reservation["provider"], reservation["number"],
                                    {"source_instance_id": reservation["instance_id"], "mode": reservation["mode"], "generation": generation,
                                     "source": source_info, "review_state": review_state}, artifacts,
                                    approved=approved, reservation_token=reservation["token"], interrupt=interrupt)
    if publish_selected:
        result["publication"] = publish_registered_engraving_run(project, registry, reservation["instance_id"], reservation["provider"], reservation["number"],
                                                               approved=approved, expected_revision=expected_revision, interrupt=interrupt)
    return result


def prepare_registered_frame(project, source, output):
    from services.frame_match import _get_video_fps
    from services.silhouette import extract_frame_as_pil
    record = source["record"]
    video = locations.project_file(project, Path(project) / "media/videos" / record["media_type"] / record["filename"])
    frame = extract_frame_as_pil(str(video), record["frame"], _get_video_fps(str(video)))
    if frame is None:
        raise ValueError("Exact registered source frame could not be decoded")
    frame.save(output, format="PNG")
    return Path(output)


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
    operation = database.execute("SELECT details FROM publication_operations WHERE operation_id=?", (operation_id,)).fetchone()
    details = json.loads(operation[0])
    if details.get("dependent_resources"):
        record = locations.find_media(store.project, binding["media_type"], media_id=binding["media_id"])
        mark_media_resources_dirty(store.project, record["filename"], binding["media_type"], "silhouette_content_changed",
                                  registry=store.registry, approved=True, edit_id=operation_id + "dirty",
                                  committed_path=store.project / details["image_path"], committed_sha256=details["image_sha256"],
                                  _resources=details["dependent_resources"])
    database.execute("UPDATE publication_operations SET state='finished' WHERE operation_id=?", (operation_id,))
    database.commit()
    _cleanup(stage)


def _validate_dirty_assessment(binding, previous, metadata, image_sha, previous_image_sha):
    from data.media_id import parse_shot_id
    old_content, new_content = dict(previous), dict(metadata)
    old_assessment = old_content.pop("source_validation", {})
    assessment = new_content.pop("source_validation", {})
    reasons = assessment.get("reasons") if isinstance(assessment, dict) else None
    if (old_content != new_content or image_sha != previous_image_sha
            or not isinstance(reasons, list) or not reasons
            or any(not isinstance(reason, str) or not reason for reason in reasons)
            or not set(old_assessment.get("reasons", [])).issubset(reasons)
            or not set(old_assessment.get("committed_edits", [])).issubset(assessment.get("committed_edits", []))
            or assessment.get("dirty") is not True
            or assessment.get("instance_id") != binding["instance_id"]
            or assessment.get("registered_shot_id") != binding["shot_id"]
            or parse_shot_id(assessment.get("current_shot_id"))[0] != binding["media_id"]):
        raise ValueError("Dirty assessment cannot change identity, content or image")


def publish_resource(project, registry, instance_id, kind, metadata, image, *, mode="", expected_revision=0,
                     approved=False, interrupt=None, _assessment_only=False, _propagation_id=None, _review_only=False, _assignment_only=False):
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
        pending = database.execute("SELECT 1 FROM publication_operations WHERE kind!='dirty-propagation' AND NOT (kind IN ('annotation-collection','silhouettes') AND state='committed' AND operation_id||'dirty'=?) AND state NOT IN ('finished','rolled_back')", (_propagation_id or "",)).fetchone()
        if pending:
            raise ValueError("Pending publication requires explicit recovery")
        binding = locations.registered_instance(project, registry, instance_id)
        if (kind != "silhouettes" or binding["locations"]
            or json.loads(binding["legacy_identity"]).get("registration_kind") != "extraction"):
            source = _silhouette(project, binding, active_only=False)
            if not (_assessment_only or _review_only or _assignment_only):
                from services.silhouette_catalog import assignment_is_active, require_active_record
                if not (kind == "silhouettes" and binding["locations"] and not assignment_is_active(source["record"])
                        and metadata.get("assignment") == source["record"].get("assignment")):
                    require_active_record(source["record"])
        if _assessment_only or _review_only or _assignment_only:
            old = locations.current_location(project, binding, kind, mode)
            if old is None:
                raise ValueError("Dirty assessment requires an existing committed pair")
            if _assignment_only:
                from services.silhouette_catalog import ASSIGNMENT_ACTIVE, ASSIGNMENT_INACTIVE, ASSIGNMENT_SUPERSEDED
                before, after = dict(old["record"]), dict(metadata)
                before.pop("assignment", None)
                after.pop("assignment", None)
                if (kind != "silhouettes" or before != after
                        or not isinstance(metadata.get("assignment"), dict)
                        or metadata["assignment"].get("state") not in (ASSIGNMENT_ACTIVE, ASSIGNMENT_INACTIVE, ASSIGNMENT_SUPERSEDED)
                        or digest(locations.project_file(project, image)) != digest(old["image"])):
                    raise ValueError("Assignment commit cannot change content, source, image or identity")
            elif _review_only:
                from services.engraving_local_publish import REVIEW_STATES
                before, after = dict(old["record"]), dict(metadata)
                for key in ("review_state", "reviewed_at"):
                    before.pop(key, None)
                    after.pop(key, None)
                if (kind != "engravings" or before != after or metadata.get("review_state") not in REVIEW_STATES
                        or not isinstance(metadata.get("reviewed_at"), str) or not metadata["reviewed_at"]
                        or digest(locations.project_file(project, image)) != digest(old["image"])):
                    raise ValueError("Review commit cannot change content, image, source or lifecycle identity")
            else:
                _validate_dirty_assessment(binding, old["record"], metadata,
                                           digest(locations.project_file(project, image)), digest(old["image"]))
            validation = {"state": "valid", "missing": [], "errors": [],
                          "destination": {"dir": old["metadata"].parent, "metadata": old["metadata"], "image": old["image"]}}
        else:
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
                   "metadata": metadata, "before": before, "assessment_only": _assessment_only, "propagation_id": _propagation_id}
        _sync(stage)
        if kind == "silhouettes" and previous is not None and details["image_sha256"] != previous["image_sha256"]:
            details["dependent_resources"] = [list(row) for row in database.execute("SELECT instance_id,kind,mode FROM resource_locations WHERE instance_id=? AND kind='engravings' ORDER BY mode", (instance_id,))]
        database.execute("INSERT INTO publication_operations VALUES(?,?,?,?,?,?)", (operation_id, instance_id, kind, mode, "prepared", json.dumps(details)))
        database.commit()
        if interrupt:
            interrupt("prepared")
        _finish(store, database, binding, operation_id, kind, mode, details, interrupt)
    return {"operation_id": operation_id, "revision": expected_revision + 1, "published": True}


def mark_media_resources_dirty(project, filename, media_type, reason, *, registry=None, approved=False,
                               edit_id=None, committed_path=None, committed_sha256=None, interrupt=None,
                               current_associations=None, _defer=False, _resources=None):
    """Explicit post-commit hook; production without registry context remains inactive."""
    if registry is None:
        return {"activated": False, "updated": 0}
    import sqlite3
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Dirty propagation requires an explicit committed-edit reason")
    store = ResourceStore(project, registry, approved=approved)
    media = locations.find_media(project, media_type, filename=filename)
    with store.transaction() as database:
        previous = database.execute("SELECT * FROM publication_operations WHERE operation_id=? AND kind='dirty-propagation'", (edit_id,)).fetchone() if edit_id else None
        if previous is not None:
            details = json.loads(previous["details"])
            if (details["media_id"], details["media_type"], details["filename"], details["reason"]) != (media["media_id"], media_type, filename, reason):
                raise ValueError("Committed edit token belongs to a different operation")
            operation_id = previous["operation_id"]
        else:
            keys = database.execute("SELECT l.instance_id,l.kind,l.mode FROM resource_locations l JOIN instances i USING(instance_id) WHERE i.media_id=? AND i.media_type=? ORDER BY l.instance_id,l.kind DESC,l.mode",
                                    (media["media_id"], media_type)).fetchall()
            if _resources is not None:
                wanted = {tuple(key) for key in _resources}
                if not wanted.issubset({tuple(key) for key in keys}):
                    raise ValueError("Dirty dependency selection differs from registered ownership")
                keys = [key for key in keys if tuple(key) in wanted]
            if not keys:
                return {"activated": True, "updated": 0, "state": "finished"}
            operation_id = edit_id or uuid4().hex
            details = {"media_id": media["media_id"], "media_type": media_type, "filename": filename, "reason": reason,
                       "resources": [list(key) for key in keys], "completed": [], "associations": current_associations or {},
                       "association_tracking": current_associations is not None}
            if committed_path is not None:
                details["committed_path"] = relative_path(project, committed_path)
                details["committed_sha256"] = committed_sha256 or digest(locations.project_file(project, committed_path))
            database.execute("INSERT INTO publication_operations VALUES(?,?,?,?,?,?)", (operation_id, keys[0][0], "dirty-propagation", "", "prepared", json.dumps(details)))
    if _defer:
        return {"activated": True, "updated": 0, "state": "prepared", "operation_id": operation_id}
    return resume_dirty_propagation(project, registry, operation_id, approved=approved, interrupt=interrupt)


def resume_dirty_propagation(project, registry, operation_id, *, approved=False, interrupt=None):
    """Resume a retained dependent set using the existing per-pair publication journal."""
    from data.resource_storage import registry_lock
    with registry_lock(project, registry, exclusive=True):
        return _resume_dirty_propagation(project, registry, operation_id, approved=approved, interrupt=interrupt)


def _resume_dirty_propagation(project, registry, operation_id, *, approved=False, interrupt=None):
    store = ResourceStore(project, registry, approved=approved)
    with store.transaction() as database:
        row = database.execute("SELECT * FROM publication_operations WHERE operation_id=? AND kind='dirty-propagation'", (operation_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown committed-edit propagation operation")
        details = json.loads(row["details"])
        if row["state"] == "finished":
            return {"activated": True, "updated": len(details["completed"]), "state": "finished", "operation_id": operation_id}
        pending = [dict(child) for child in database.execute("SELECT * FROM publication_operations WHERE kind NOT IN ('dirty-propagation','annotation-collection') AND state NOT IN ('finished','rolled_back')")
               if not (child["kind"] == "silhouettes" and child["state"] == "committed" and child["operation_id"] + "dirty" == operation_id)]
    if details.get("committed_path") and digest(locations.project_file(project, details["committed_path"])) != details["committed_sha256"]:
        raise ValueError("Committed edit changed or was not committed before dirty propagation")
    for child in pending:
        if json.loads(child["details"]).get("propagation_id") != operation_id:
            raise ValueError("Unrelated pending publication requires explicit recovery")
        recover_publication(project, registry, child["operation_id"], approved=approved)
    for key in details["resources"]:
        if key in details["completed"]:
            continue
        instance_id, kind, mode = key
        binding = locations.registered_instance(project, registry, instance_id)
        current = locations.current_location(project, binding, kind, mode)
        assessment = dict(current["record"].get("source_validation") or {})
        reasons = list(assessment.get("reasons") or [])
        if details["reason"] not in reasons:
            reasons.append(details["reason"])
        edits = list(assessment.get("committed_edits") or [])
        if operation_id not in edits:
            edits.append(operation_id)
            assessment.update({"dirty": True, "instance_id": instance_id, "registered_shot_id": binding["shot_id"],
                               "current_shot_id": details["associations"].get(instance_id) or assessment.get("current_shot_id") or binding["shot_id"],
                               "reasons": reasons, "committed_edits": edits})
            if details.get("association_tracking"):
                assessment["current_association_known"] = instance_id in details["associations"]
            publish_resource(project, registry, instance_id, kind, {**current["record"], "source_validation": assessment}, current["image"], mode=mode,
                             expected_revision=current["revision"], approved=approved, _assessment_only=True,
                             _propagation_id=operation_id, interrupt=interrupt)
        with store.transaction() as database:
            details["completed"].append(key)
            database.execute("UPDATE publication_operations SET details=? WHERE operation_id=?", (json.dumps(details), operation_id))
        if interrupt:
            interrupt("dependent-completed")
    with store.transaction() as database:
        database.execute("UPDATE publication_operations SET state='finished' WHERE operation_id=?", (operation_id,))
    return {"activated": True, "updated": len(details["completed"]), "state": "finished", "operation_id": operation_id}


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
        operation = database.execute("SELECT kind FROM publication_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if operation is not None and operation["kind"] == "dirty-propagation":
        if action != "complete":
            raise ValueError("Committed-edit intent cannot be implicitly rolled back; preserve and resume it")
        return resume_dirty_propagation(project, registry, operation_id, approved=approved)
    if operation is not None and operation["kind"] == "annotation-collection":
        if action != "complete":
            raise ValueError("Collection rollback requires explicit intervention; preserve and resume the edit")
        return resume_annotation_collection(project, registry, operation_id, approved=approved)
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
            if details.get("assessment_only") is True:
                previous = locations.project_file(project, stage / "previous.json")
                if digest(previous) != details["before"].get("metadata"):
                    raise ValueError("Dirty assessment before-image corrupted")
                _validate_dirty_assessment(binding, json.loads(previous.read_text(encoding="utf-8")),
                                           details["metadata"], details["image_sha256"], details["before"].get("image"))
            else:
                validation = _validation(store, database, binding, details["metadata"], row["kind"], row["mode"])
                if validation["state"] != "valid":
                    raise ValueError("Recovery requires valid source/publication metadata")
            _finish(store, database, binding, operation_id, row["kind"], row["mode"], details)
        return {"operation_id": operation_id, "state": "rolled_back" if action == "rollback" else "finished"}


def _validate_collection_payload(project, media, paths, payloads, changed, *, media_type):
    import hashlib
    import io
    import numpy as np
    from data.media_id import parse_shot_id
    from services.resource_validation import validate_annotation_classifications
    entries = json.loads(payloads[".annotations.json"])
    if not isinstance(entries, list):
        raise ValueError("Annotation collection requires a shot list")
    identities = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("shot"), dict):
            raise ValueError("Annotation entry requires a shot object")
        shot = entry["shot"]
        identity = shot.get("shot_id")
        source_media, start, end = parse_shot_id(identity)
        if source_media != media["media_id"] or identity in identities:
            raise ValueError("Annotation shot identity mismatch or duplicate")
        identities.add(identity)
        if shot.get("best_frame") is not None:
            frame = shot["best_frame"].get("frame") if isinstance(shot["best_frame"], dict) else None
            if type(frame) is not int or not start <= frame <= end:
                raise ValueError("Best-frame source frame is outside the registered shot")
        if shot.get("annotation") is not None:
            findings = validate_annotation_classifications(shot["annotation"])
            if findings:
                raise ValueError("Annotation classification validation failed: " + json.dumps(findings))
    families = [(".frames.manifest.json", {"npy": ".frames.npy", "valid": ".frames.valid.npy"}),
                (".annotations.manifest.json", {"npy": ".annotations.npy", "txt": ".annotations.txt"})]
    for suffix, sections in families:
        family = {suffix, *sections.values()}
        if not family.intersection(changed):
            continue
        if suffix not in payloads and not suffix.startswith(".frames"):
            for member in sections.values():
                if member not in changed:
                    continue
                if member.endswith(".npy"):
                    array = np.load(io.BytesIO(payloads[member]), allow_pickle=False)
                    if array.ndim != 2 or array.shape[0] != len(entries) or array.dtype != np.dtype("float32") or not np.isfinite(array).all():
                        raise ValueError("Optional annotation embedding shape/dtype mismatch")
                    if paths[member].exists():
                        previous = np.load(locations.project_file(project, paths[member]), allow_pickle=False)
                        if previous.shape != array.shape or previous.dtype != array.dtype:
                            raise ValueError("Existing array shape/dtype must be preserved")
                else:
                    payloads[member].decode("utf-8")
            continue
        if suffix not in payloads or (suffix.startswith(".frames") and not family.issubset(changed)):
            raise ValueError("Frame/embedding update requires its array/mask/manifest pair")
        manifest = json.loads(payloads[suffix])
        if (manifest.get("version") != 1 or manifest.get("media_type") != media_type
                or manifest.get("index_type") != ("frame-embeddings" if suffix.startswith(".frames") else "annotation-embeddings")):
            raise ValueError("Manifest type/version/media association mismatch")
        source = manifest.get("json", {})
        if (source.get("hash") != "sha256:" + hashlib.sha256(payloads[".annotations.json"]).hexdigest()
                or source.get("item_count") != len(entries)
                or source.get("path") != relative_path(project, paths[".annotations.json"])
                or source.get("filename") != paths[".annotations.json"].name):
            raise ValueError("Manifest annotation source mismatch")
        arrays = {}
        for section, member in sections.items():
            if member not in payloads:
                raise ValueError("Manifest member missing")
            description = manifest.get(section, {})
            if (description.get("hash") != "sha256:" + hashlib.sha256(payloads[member]).hexdigest()
                    or description.get("path") != relative_path(project, paths[member])
                    or description.get("filename") != paths[member].name):
                raise ValueError("Manifest artifact hash/path mismatch")
            if member.endswith(".npy"):
                array = np.load(io.BytesIO(payloads[member]), allow_pickle=False)
                arrays[section] = array
                if description.get("shape") != list(array.shape) or description.get("dtype") != str(array.dtype):
                    raise ValueError("Manifest array shape/dtype mismatch")
                if paths[member].exists():
                    previous = np.load(locations.project_file(project, paths[member]), allow_pickle=False)
                    if previous.shape != array.shape or previous.dtype != array.dtype:
                        raise ValueError("Existing array shape/dtype must be preserved")
            elif description.get("line_count") != len([line for line in payloads[member].decode("utf-8").splitlines() if line.strip()]):
                raise ValueError("Annotation text manifest line count mismatch")
        vectors = arrays["npy"]
        if (vectors.ndim != 2 or vectors.shape[0] != len(entries) or vectors.dtype != np.dtype("float32")
                or not np.isfinite(vectors).all()):
            raise ValueError("Embedding array shape/dtype/source correspondence mismatch")
        if "valid" in arrays:
            valid = arrays["valid"]
            if valid.dtype != np.dtype("bool") or valid.shape != (len(entries),):
                raise ValueError("Frame validity mask mismatch")
            counts = manifest.get("frames", {})
            if counts.get("valid_count") != int(valid.sum()) or counts.get("missing_count") != int((~valid).sum()):
                raise ValueError("Frame manifest validity counts mismatch")
            if np.any(vectors[~valid] != 0):
                raise ValueError("Invalid frame rows must remain zero vectors")
    return entries


def publish_annotation_collection(project, registry, filename, media_type, changes, *, expected_revision,
                                  expected_hashes, approved=False, operation_id=None, interrupt=None):
    """CAS publish selected collection members through the existing journal."""
    import hashlib
    from data.resource_storage import registry_lock
    store = ResourceStore(project, registry, approved=approved)
    if type(expected_revision) is not int or expected_revision < 1 or not changes:
        raise ValueError("Committed collection revision and changed artifacts required")
    if any(suffix not in locations.ANNOTATION_SUFFIXES for suffix in changes):
        raise ValueError("Unknown annotation artifact")
    proposed = {suffix: value if isinstance(value, bytes) else locations.project_file(project, value).read_bytes()
                for suffix, value in changes.items()}
    hashes = {suffix: hashlib.sha256(value).hexdigest() for suffix, value in proposed.items()}
    with registry_lock(project, registry, exclusive=True):
        with store.transaction() as database:
            if next(row for row in database.execute("PRAGMA table_info(publication_operations)")
                    if row["name"] == "instance_id")["notnull"]:
                raise ValueError("Explicit collection journal upgrade required")
            existing = database.execute("SELECT * FROM publication_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if existing is not None:
                details = json.loads(existing["details"])
                if (existing["kind"] != "annotation-collection" or details["filename"] != filename
                        or details["media_type"] != media_type or details["proposed"] != hashes
                        or details["expected_revision"] != expected_revision or details["before"] != expected_hashes):
                    raise ValueError("Operation token belongs to a different collection edit")
            else:
                if database.execute("SELECT 1 FROM publication_operations WHERE state NOT IN ('finished','rolled_back')").fetchone():
                    raise ValueError("Pending publication requires explicit recovery")
                media = locations.find_media(project, media_type, filename=filename)
                row = database.execute("SELECT * FROM annotation_layouts WHERE media_type=? AND media_id=?",
                                       (media_type, media["media_id"])).fetchone()
                if row is None or row["filename"] != filename:
                    raise ValueError("Explicit committed annotation collection required")
                before = json.loads(row["artifacts"])
                if row["revision"] != expected_revision or before != expected_hashes:
                    raise ValueError("Annotation revision/hash conflict")
                paths = {suffix: locations.annotation_path(project, filename, media_type, suffix, layout=row["layout"])
                         for suffix in set(before) | set(proposed)}
                payloads = {}
                for suffix, sha in before.items():
                    path = locations.project_file(project, paths[suffix])
                    if digest(path) != sha:
                        raise ValueError("Committed annotation artifact checksum mismatch")
                    payloads[suffix] = path.read_bytes()
                for suffix in set(proposed) - set(before):
                    if paths[suffix].exists():
                        raise ValueError("Uncommitted annotation destination already exists")
                payloads.update(proposed)
                _validate_collection_payload(project, media, paths, payloads, proposed, media_type=media_type)
                changed = {suffix: value for suffix, value in proposed.items() if hashes[suffix] != before.get(suffix)}
                if not changed:
                    return {"revision": expected_revision, "published": False}
                operation_id = operation_id or uuid4().hex
                if not isinstance(operation_id, str) or not operation_id.isalnum():
                    raise ValueError("Invalid collection operation token")
                stage = store.registry.parent / ".resource-publications" / operation_id
                create_storage_directory(project, stage, exclusive=True)
                for suffix, value in changed.items():
                    path = stage / ("new" + suffix)
                    path.write_bytes(value)
                    _sync(path)
                    if suffix in before:
                        _copy(paths[suffix], stage / ("previous" + suffix))
                _sync(stage)
                details = {"stage": relative_path(project, stage), "filename": filename, "media_type": media_type,
                           "media_id": media["media_id"], "layout": row["layout"], "expected_revision": expected_revision,
                           "before": before, "proposed": hashes, "changed": {suffix: hashes[suffix] for suffix in changed},
                           "artifacts": {**before, **hashes}}
                database.execute("INSERT INTO publication_operations VALUES(?,?,?,?,?,?)",
                                 (operation_id, None, "annotation-collection", "", "prepared", json.dumps(details)))
        if interrupt and existing is None:
            interrupt("prepared")
        return resume_annotation_collection(project, registry, operation_id, approved=approved, interrupt=interrupt)


def resume_annotation_collection(project, registry, operation_id, *, approved=False, interrupt=None):
    from data.resource_storage import registry_lock
    store = ResourceStore(project, registry, approved=approved)
    with registry_lock(project, registry, exclusive=True):
        with store.transaction() as database:
            row = database.execute("SELECT * FROM publication_operations WHERE operation_id=? AND kind='annotation-collection'",
                                   (operation_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown collection operation")
            details = json.loads(row["details"])
            state = row["state"]
            if state == "rolled_back":
                raise ValueError("Rolled-back collection operation cannot be resumed")
            media = locations.find_media(project, details["media_type"], filename=details["filename"])
            if media["media_id"] != details["media_id"]:
                raise ValueError("Collection media association changed; explicit intervention required")
            stage = store.registry.parent / ".resource-publications" / operation_id
            if relative_path(project, stage) != details["stage"]:
                raise ValueError("Collection staging ownership mismatch")
            paths = {suffix: locations.annotation_path(project, details["filename"], details["media_type"], suffix,
                                                      layout=details["layout"]) for suffix in details["artifacts"]}
            current = database.execute("SELECT * FROM annotation_layouts WHERE media_type=? AND media_id=?",
                                       (details["media_type"], details["media_id"])).fetchone()
            committed = state in ("committed", "finished")
            if (current is None or current["layout"] != details["layout"] or current["filename"] != details["filename"]
                    or current["revision"] != details["expected_revision"] + int(committed)
                    or json.loads(current["artifacts"]) != (details["artifacts"] if committed else details["before"])):
                raise ValueError("Collection recovery revision/hash conflict")
            payloads = {}
            for suffix, sha in details["artifacts"].items():
                path = stage / ("new" + suffix) if suffix in details["changed"] and not committed else paths[suffix]
                path = locations.project_file(project, path)
                if digest(path) != sha:
                    raise ValueError("Collection recovery artifact checksum mismatch")
                payloads[suffix] = path.read_bytes()
                target = paths[suffix]
                if not committed and suffix in details["changed"]:
                    allowed = {details["changed"][suffix], details["before"].get(suffix)}
                    if target.exists() and digest(locations.project_file(project, target)) not in allowed:
                        raise ValueError("Recovery refuses unrelated destination content")
                    if suffix in details["before"] and (not target.exists() or digest(stage / ("previous" + suffix)) != details["before"][suffix]):
                        raise ValueError("Collection before-image missing or corrupt")
            _validate_collection_payload(project, media, paths, payloads, details["proposed"], media_type=details["media_type"])
            if state == "finished":
                return {"operation_id": operation_id, "revision": details["expected_revision"] + 1, "published": True}
            if not committed:
                database.execute("UPDATE publication_operations SET state='installing' WHERE operation_id=?", (operation_id,))
                database.commit()
                for suffix in sorted(details["changed"]):
                    temporary = paths[suffix].parent / (".publication-" + operation_id + suffix)
                    _copy(stage / ("new" + suffix), temporary)
                    os.replace(temporary, paths[suffix])
                    _sync(paths[suffix].parent)
                    if interrupt:
                        interrupt(suffix + "-installed")
                database.execute("UPDATE publication_operations SET state='files_published' WHERE operation_id=?", (operation_id,))
                database.commit()
                if any(digest(paths[suffix]) != sha for suffix, sha in details["artifacts"].items()):
                    raise ValueError("Installed collection checksum mismatch")
                if interrupt:
                    interrupt("before-commit")
                store.commit_annotation_update(database, operation_id)
                database.execute("UPDATE publication_operations SET state='committed' WHERE operation_id=?", (operation_id,))
                database.commit()
                if interrupt:
                    interrupt("committed")
        from services.illustration_index import invalidate_index
        for kind in ("silhouettes", "engravings"):
            invalidate_index(project, kind, details["media_type"])
        if interrupt:
            interrupt("invalidated")
        mark_media_resources_dirty(project, details["filename"], details["media_type"], "annotation collection edited",
                                  registry=registry, approved=approved, edit_id=operation_id + "dirty",
                                  committed_path=paths[".annotations.json"], committed_sha256=details["artifacts"][".annotations.json"],
                                  interrupt=interrupt)
        with store.transaction() as database:
            if any(digest(paths[suffix]) != sha for suffix, sha in details["artifacts"].items()):
                raise ValueError("Completed collection checksum mismatch")
            database.execute("UPDATE publication_operations SET state='finished' WHERE operation_id=?", (operation_id,))
        return {"operation_id": operation_id, "revision": details["expected_revision"] + 1, "published": True}


def publication_status(project, registry, *, instance_id=None):
    """Operational read of pending/completed journals, including interrupted installs."""
    import sqlite3
    path = locations.project_file(project, registry)
    with registry_lock(project, registry, exclusive=True), sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute("SELECT * FROM publication_operations WHERE (? IS NULL OR instance_id=?) ORDER BY operation_id", (instance_id, instance_id))
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]