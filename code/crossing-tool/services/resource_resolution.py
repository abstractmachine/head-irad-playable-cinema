"""Exact read-only resource resolution; discovery remains Illustration-index owned."""

from data import resource_locations as locations
from data import resource_locations_legacy as legacy


def _silhouette(project, binding):
    from services.silhouette_catalog import require_active_record
    result = locations.current_location(project, binding, "silhouettes", "")
    result = result if result is not None else legacy.registered_silhouette(project, binding)
    require_active_record(result["record"], source=result["metadata"], project_path=project)
    return result


def resolve_silhouette(project, instance_id, *, registry):
    """Resolve an active registered instance without allocation or directory search."""
    return _silhouette(project, locations.registered_instance(project, registry, instance_id))


def resolve_engraving(project, instance_id, mode="frame", *, registry):
    """Resolve a generated mode publication, preserving canonical source authorization."""
    if mode not in locations.MODES:
        raise ValueError("Unsupported engraving mode")
    binding = locations.registered_instance(project, registry, instance_id)
    _silhouette(project, binding)
    result = locations.current_location(project, binding, "engravings", mode)
    result = result if result is not None else legacy.registered_engraving(project, binding, mode)
    if result["record"].get("status") != "generated":
        raise ValueError("Engraving is not a generated publication")
    return result