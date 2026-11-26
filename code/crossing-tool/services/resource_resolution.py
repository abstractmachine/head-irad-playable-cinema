"""Exact read-only resource resolution; discovery remains Illustration-index owned."""

from data import resource_locations as locations
from data import resource_locations_legacy as legacy
from tool.project_access import project_operation


def _silhouette(project, binding, *, active_only=True):
    from services.silhouette_catalog import require_active_record
    result = locations.current_location(project, binding, "silhouettes", "")
    if result is None:
        from tool.project_access import canonical_registry
        if canonical_registry(project) is not None:
            raise ValueError("Canonical silhouette location missing; legacy fallback forbidden")
    result = result if result is not None else legacy.registered_silhouette(project, binding)
    if active_only:
        require_active_record(result["record"], source=result["metadata"], project_path=project)
    return result


@project_operation
def resolve_silhouette(project, instance_id, *, registry=None, active_only=True):
    """Resolve an active registered instance without allocation or directory search."""
    from data.resource_storage import registry_lock
    if registry is None:
        raise ValueError("Registered source identity requires a canonical registry")
    with registry_lock(project, registry):
        result = _silhouette(project, locations.registered_instance(project, registry, instance_id), active_only=active_only)
        result["image_bytes"] = result["image"].read_bytes()
        return result


@project_operation
def resolve_source_silhouette(project, source_json=None, *, registry=None, instance_id=None):
    """Shared producer source interface: exact registered identity or current legacy input."""
    from pathlib import Path
    from services.silhouette_catalog import load_active_record
    from services.engraving_paths import resolve_silhouette_png
    if registry is not None:
        if instance_id is None:
            import sqlite3
            from data.resource_storage import registry_lock, relative_path
            reference = relative_path(project, source_json)
            with registry_lock(project, registry), sqlite3.connect(locations.project_file(project, registry).as_uri() + "?mode=ro", uri=True) as database:
                rows = database.execute("SELECT instance_id FROM instances WHERE legacy_json=? UNION SELECT instance_id FROM resource_locations WHERE kind='silhouettes' AND record_path=?", (reference, reference)).fetchall()
                if len(rows) != 1:
                    raise ValueError("Source path has no exact unique registered binding")
                instance_id = rows[0][0]
        return resolve_silhouette(project, instance_id, registry=registry)
    if instance_id is not None:
        raise ValueError("Registered source identity requires an explicit registry")
    path = Path(source_json)
    record = load_active_record(path, project)
    return {"metadata": path, "image": resolve_silhouette_png(path, record), "record": record, "layout": "legacy-v1",
            "instance_id": record.get("instance_id")}


@project_operation
def resolve_engraving(project, instance_id, mode="frame", *, registry=None, active_only=True):
    """Resolve a generated mode publication, preserving canonical source authorization."""
    if mode not in locations.MODES:
        raise ValueError("Unsupported engraving mode")
    from data.resource_storage import registry_lock
    with registry_lock(project, registry):
        binding = locations.registered_instance(project, registry, instance_id)
        source = _silhouette(project, binding, active_only=active_only)
        result = locations.current_location(project, binding, "engravings", mode)
        if result is None:
            from tool.project_access import canonical_registry
            if canonical_registry(project) is not None:
                raise ValueError("Canonical engraving location missing; legacy fallback forbidden")
        result = result if result is not None else legacy.registered_engraving(project, binding, mode)
        if result["record"].get("status") != "generated":
            raise ValueError("Engraving is not a generated publication")
        result["source_dirty"] = bool(source["record"].get("source_validation", {}).get("dirty") or result["record"].get("source_validation", {}).get("dirty"))
        result["image_bytes"] = result["image"].read_bytes()
        return result


@project_operation
def indexed_publications(project, kind, media_type, *, registry=None):
    """Materialize committed publications for an explicitly requested derived rebuild."""
    from contextlib import closing
    import sqlite3
    from data.resource_storage import registry_lock, relative_path
    if kind not in ("silhouettes", "engravings"):
        raise ValueError("Unsupported illustration source")
    with registry_lock(project, registry):
        with closing(sqlite3.connect(locations.project_file(project, registry).as_uri() + "?mode=ro", uri=True)) as database, database:
            keys = database.execute("SELECT l.instance_id,l.mode FROM resource_locations l JOIN instances i USING(instance_id) WHERE l.kind=? AND i.media_type=? ORDER BY l.instance_id,l.mode",
                                    (kind, media_type)).fetchall()
    records = []
    for instance_id, mode in keys:
        resolved = resolve_silhouette(project, instance_id, registry=registry, active_only=False) if kind == "silhouettes" else resolve_engraving(project, instance_id, mode, registry=registry, active_only=False)
        record = {**resolved["record"], "path": resolved["metadata"], "output_png": resolved["image"],
                  "instance_id": instance_id, "object_id": instance_id, "resource_kind": kind,
                  "location_revision": resolved["revision"], "resource_registry": relative_path(project, registry)}
        records.append(record)
    return records


@project_operation
def resolve_index_record(project, record, *, active_only=True):
    """Verify an indexed registered reference, never substitute an unregistered path."""
    from pathlib import Path
    registry = record.get("resource_registry")
    if registry is None:
        from tool.project_access import canonical_registry
        if canonical_registry(project) is not None:
            raise ValueError("Canonical index record lacks registry provenance")
        return record
    instance_id = record["instance_id"]
    resolved = (resolve_silhouette(project, instance_id, registry=registry, active_only=active_only) if record["resource_kind"] == "silhouettes"
                else resolve_engraving(project, instance_id, record["mode"], registry=registry, active_only=active_only))
    if (Path(record["path"]) != resolved["metadata"] or record.get("location_revision") != resolved["revision"]
            or any(record.get(key) != resolved["record"].get(key) for key in ("field", "label"))):
        raise ValueError("Derived illustration index points to a stale registered location")
    if record["resource_kind"] == "silhouettes":
        from services.silhouette_catalog import assignment_state_for_record
        if assignment_state_for_record(record) != assignment_state_for_record(resolved["record"]):
            raise ValueError("Derived illustration index points to a stale registered assignment")
    return {**record, "path": resolved["metadata"], "output_png": resolved["image"]}