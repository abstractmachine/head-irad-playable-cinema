"""Curation persistence helpers for the silhouette catalog.

Provides the persistence layer for human-best selection: marking, unmarking,
querying, and utility functions that the visualizer and tests can share.

The ``human_best`` field is written directly into the canonical object JSON
alongside all other scoring data.  When ``human_best`` is absent or ``False``
the object is not marked as human-selected; the field is removed rather than
set to ``False`` to keep JSONs backward compatible.

Assignment lifecycle is independent from both ``human_best`` and
``search_provenance``. A curator can de-assign an object without removing its
PNG/JSON: the object becomes inactive and carries a pending recheck request in
its own assignment metadata.
"""

from __future__ import annotations

from pathlib import Path
import json
from datetime import datetime, timezone
from typing import Any

from services.silhouette_catalog import (
    ASSIGNMENT_ACTIVE,
    ASSIGNMENT_FIELD,
    ASSIGNMENT_INACTIVE,
    ASSIGNMENT_SUPERSEDED,
    RECHECK_COMPLETED,
    RECHECK_FIELD,
    RECHECK_NO_RESULT,
    RECHECK_PENDING,
    active_assignment,
    assignment_is_active,
    assignment_state_for_record,
    catalog_object_reference,
    extraction_identity,
    same_extraction_identity,
)

HUMAN_BEST_FIELD = "human_best"
CURATORIAL_REJECTION = "curatorial_rejection"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_record(json_path: str | Path) -> tuple[Path, dict[str, Any]]:
    path = Path(json_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Catalog JSON must contain an object: {path}")
    data["path"] = path
    return path, data


def _persist_record(json_path: Path, record: dict[str, Any]) -> None:
    data = dict(record)
    data.pop("path", None)
    from data.annotate import atomic_write_text

    atomic_write_text(json_path, json.dumps(data, indent=2, ensure_ascii=False))
    try:
        from services.illustration_index import invalidate_for_record
        invalidate_for_record(json_path, "silhouettes")
    except Exception:
        pass


def _pending_recheck(record: dict[str, Any]) -> dict[str, Any] | None:
    assignment = record.get(ASSIGNMENT_FIELD)
    if not isinstance(assignment, dict):
        return None
    recheck = assignment.get(RECHECK_FIELD)
    return recheck if isinstance(recheck, dict) else None


def pending_recheck_matches(
    record: dict[str, Any],
    *,
    media_type: str,
    media_id: str,
    shot_id: str,
    field: str,
    search_label: str,
) -> bool:
    """Return whether a pending recheck matches one exact extraction request."""
    recheck = _pending_recheck(record)
    if assignment_state_for_record(record) != ASSIGNMENT_INACTIVE:
        return False
    if not recheck or recheck.get("state") != RECHECK_PENDING:
        return False
    requested_identity = {
        "media_type": media_type,
        "media_id": media_id,
        "shot_id": shot_id,
        "field": field,
        "search_label": search_label,
    }
    return extraction_identity(record) == requested_identity and all(
        str(recheck.get(key) or "") == value
        for key, value in requested_identity.items()
    )


def mark_recheck_pending(
    json_path: str | Path,
    *,
    annotation_value: str,
) -> dict[str, Any]:
    """De-assign one object and embed its bounded, original-label recheck.

    The historical PNG is never touched. ``annotation_value`` is required
    because catalog extraction JSON records historical search labels but does
    not universally persist one canonical original annotation value.
    """
    path, record = _load_json_record(json_path)
    if assignment_state_for_record(record) == ASSIGNMENT_SUPERSEDED:
        raise ValueError("Cannot request a recheck for a superseded silhouette")
    if not annotation_value.strip():
        raise ValueError("annotation_value is required for a recheck request")

    identity = extraction_identity(record)
    if not all(identity.values()):
        raise ValueError("Catalog record does not contain a complete extraction identity")
    assignment = dict(record.get(ASSIGNMENT_FIELD) or {})
    assignment.update({
        "state": ASSIGNMENT_INACTIVE,
        "reason": CURATORIAL_REJECTION,
        RECHECK_FIELD: {
            "state": RECHECK_PENDING,
            "requested_at": _now(),
            "annotation_value": annotation_value,
            **identity,
        },
    })
    record[ASSIGNMENT_FIELD] = assignment
    _persist_record(path, record)
    return record


def get_pending_rechecks(
    project_path: str | Path,
    *,
    media_type: str | None = None,
) -> list[dict[str, Any]]:
    """Read pending rechecks stored in canonical catalog JSON records."""
    from services.silhouette_catalog import scan_catalog

    media_types = (media_type,) if media_type else ("movie", "gameplay")
    requests: list[dict[str, Any]] = []
    for current_media_type in media_types:
        for record in scan_catalog(str(project_path), media_type=current_media_type):
            if "error" in record:
                continue
            identity = extraction_identity(record)
            if pending_recheck_matches(record, **identity):
                recheck = _pending_recheck(record) or {}
                requests.append({
                    "catalog_json_path": str(record["path"]),
                    "catalog_object": catalog_object_reference(record),
                    "identity": identity,
                    "annotation_value": str(recheck["annotation_value"]),
                    "requested_at": str(recheck["requested_at"]),
                    "reason": str((record.get(ASSIGNMENT_FIELD) or {}).get("reason") or ""),
                })
    return sorted(requests, key=lambda item: (item["requested_at"], item["catalog_json_path"]))


def mark_recheck_no_result(json_path: str | Path) -> dict[str, Any]:
    """Record a completed recheck that found no acceptable replacement."""
    path, record = _load_json_record(json_path)
    recheck = _pending_recheck(record)
    if assignment_state_for_record(record) != ASSIGNMENT_INACTIVE or not recheck:
        raise ValueError("Catalog record has no inactive recheck request")
    if recheck.get("state") != RECHECK_PENDING:
        raise ValueError("Recheck request is not pending")
    recheck["state"] = RECHECK_NO_RESULT
    recheck["completed_at"] = _now()
    record[ASSIGNMENT_FIELD][RECHECK_FIELD] = recheck
    _persist_record(path, record)
    return record


def complete_recheck(
    project_path: str | Path,
    *,
    old_json_path: str | Path,
    new_json_path: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Activate one selected replacement and preserve prior objects as superseded.

    All active records for the exact extraction identity are superseded, so the
    selected replacement is the sole active assignment without deleting any
    historical asset. Each canonical JSON mutation uses the shared atomic
    writer; cross-file updates intentionally remain an explicit small sequence.
    """
    old_path, old_record = _load_json_record(old_json_path)
    new_path, new_record = _load_json_record(new_json_path)
    if old_path == new_path:
        raise ValueError("Replacement must be a distinct catalog object")
    if not same_extraction_identity(old_record, new_record):
        raise ValueError("Replacement does not match the original extraction identity")
    old_recheck = _pending_recheck(old_record)
    if assignment_state_for_record(old_record) != ASSIGNMENT_INACTIVE or not old_recheck:
        raise ValueError("Original silhouette is not an inactive recheck request")
    if old_recheck.get("state") != RECHECK_PENDING:
        raise ValueError("Original recheck request is not pending")

    new_reference = catalog_object_reference(new_record, new_path)
    old_reference = catalog_object_reference(old_record, old_path)
    new_assignment = active_assignment()
    new_assignment["supersedes"] = old_reference
    new_record[ASSIGNMENT_FIELD] = new_assignment
    _persist_record(new_path, new_record)

    from services.silhouette_catalog import scan_catalog

    same_identity_active_paths: list[Path] = []
    identity = extraction_identity(old_record)
    for record in scan_catalog(str(project_path), media_type=identity["media_type"]):
        if "error" in record or Path(record.get("path") or "") == new_path:
            continue
        if assignment_is_active(record) and same_extraction_identity(record, old_record):
            same_identity_active_paths.append(Path(record["path"]))

    old_assignment = dict(old_record.get(ASSIGNMENT_FIELD) or {})
    old_recheck["state"] = RECHECK_COMPLETED
    old_recheck["completed_at"] = _now()
    old_assignment.update({
        "state": ASSIGNMENT_SUPERSEDED,
        "superseded_by": new_reference,
        RECHECK_FIELD: old_recheck,
    })
    old_assignment.pop("reason", None)
    old_record[ASSIGNMENT_FIELD] = old_assignment
    _persist_record(old_path, old_record)

    for active_path in same_identity_active_paths:
        if active_path == old_path:
            continue
        duplicate_path, duplicate_record = _load_json_record(active_path)
        duplicate_assignment = dict(duplicate_record.get(ASSIGNMENT_FIELD) or {})
        duplicate_assignment.update({
            "state": ASSIGNMENT_SUPERSEDED,
            "superseded_by": new_reference,
        })
        duplicate_assignment.pop("reason", None)
        duplicate_record[ASSIGNMENT_FIELD] = duplicate_assignment
        _persist_record(duplicate_path, duplicate_record)

    return old_record, new_record


def label_bucket(label: str) -> str:
    """Return the alphabetical bucket key for *label*.

    Returns the uppercase first letter, or ``'#'`` for non-letter labels.

    >>> label_bucket("horse")
    'H'
    >>> label_bucket("123")
    '#'
    >>> label_bucket("")
    '#'
    """
    if not label:
        return "#"
    first = label[0].upper()
    return first if first.isalpha() else "#"


def set_human_best(json_path: str | Path, *, human_best: bool = True) -> None:
    """Write or remove the ``human_best`` flag in a catalog object JSON.

    When *human_best* is ``True``, ``"human_best": true`` is written.
    When ``False``, the field is removed entirely (backward compatible).

    Raises ``FileNotFoundError`` if the JSON file does not exist.
    """
    json_path = Path(json_path)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if human_best:
        data[HUMAN_BEST_FIELD] = True
    else:
        data.pop(HUMAN_BEST_FIELD, None)
    from data.annotate import atomic_write_text

    atomic_write_text(json_path, json.dumps(data, indent=2, ensure_ascii=False))
    try:
        from services.illustration_index import invalidate_for_record
        invalidate_for_record(json_path, "silhouettes")
    except Exception:
        pass


def clear_human_best_for_label(
    all_label_records: list[dict],
    except_path: str | Path | None = None,
) -> None:
    """Clear ``human_best`` from every record in a label group, optionally excluding one.

    Only records that currently have ``human_best: true`` are written to disk.
    In-memory record dicts are updated in place.

    Parameters
    ----------
    all_label_records:
        Full list of catalog records for the target label (across all films).
    except_path:
        JSON file path to leave untouched.  Pass the target record's path when
        transferring the best marker to a new object.
    """
    except_str = str(except_path) if except_path else None
    for rec in all_label_records:
        if not rec.get(HUMAN_BEST_FIELD):
            continue
        rec_path = rec.get("path")
        if rec_path is None:
            continue
        if except_str and str(rec_path) == except_str:
            continue
        json_path = Path(rec_path)
        if not json_path.exists():
            continue
        try:
            set_human_best(json_path, human_best=False)
            rec.pop(HUMAN_BEST_FIELD, None)
        except Exception:
            pass


def mark_best(target_rec: dict, all_label_records: list[dict]) -> bool:
    """Mark *target_rec* as human-best.

    Multiple objects in the same label group can be marked simultaneously —
    this call does NOT clear other records' ``human_best`` flags.
    Both in-memory and on-disk state are updated (best-effort).

    Returns ``True`` on success, ``False`` when the target JSON is invalid.
    """
    target_path = target_rec.get("path")
    if not target_path or not Path(target_path).exists():
        return False

    try:
        set_human_best(target_path, human_best=True)
        target_rec[HUMAN_BEST_FIELD] = True
        return True
    except Exception:
        return False


def unmark_best(rec: dict) -> bool:
    """Remove the ``human_best`` marker from *rec*.

    Updates both the in-memory dict and the on-disk JSON.
    Returns ``True`` if a marker was present (and removed), ``False`` otherwise.
    """
    if not rec.get(HUMAN_BEST_FIELD):
        return False
    json_path = Path(rec.get("path", ""))
    if not json_path.exists():
        return False
    try:
        set_human_best(json_path, human_best=False)
        rec.pop(HUMAN_BEST_FIELD, None)
        return True
    except Exception:
        return False


def find_best_in_records(records: list[dict]) -> int:
    """Return the index of the first record with ``human_best=True``, or ``-1``."""
    for i, rec in enumerate(records):
        if rec.get(HUMAN_BEST_FIELD):
            return i
    return -1
