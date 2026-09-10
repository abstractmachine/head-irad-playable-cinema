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
from contextlib import contextmanager
from typing import Any

from services.silhouette_catalog import (
    ASSIGNMENT_ACTIVE,
    ASSIGNMENT_FIELD,
    ASSIGNMENT_INACTIVE,
    ASSIGNMENT_SUPERSEDED,
    RECHECK_COMPLETED,
    RECHECK_ERROR,
    RECHECK_FIELD,
    RECHECK_NO_RESULT,
    RECHECK_PENDING,
    active_assignment,
    assignment_is_active,
    assignment_state_for_record,
    catalog_object_reference,
    extraction_identity,
    iter_catalog,
    same_extraction_identity,
)

HUMAN_BEST_FIELD = "human_best"
CURATORIAL_REJECTION = "curatorial_rejection"
SOURCE_RECHECK_STAGED = "source_recheck_staged"
SOURCE_RECHECK_JOB_FIELD = "source_recheck_job"


class SourceJobCommitInterrupted(KeyboardInterrupt):
    """SIGINT delivered immediately after a complete source-job persistence pass."""

    def __init__(
        self,
        historical: list[dict[str, Any]],
        replacements: list[dict[str, Any]],
    ) -> None:
        super().__init__("Source job committed before SIGINT delivery")
        self.historical = historical
        self.replacements = replacements


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _defer_sigint_during_source_commit():
    """Deliver Ctrl-C after, rather than during, one multi-file source commit."""
    try:
        import signal
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    except (AttributeError, OSError):
        yield
        return
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


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
    # Automated rechecks carry separate current source values and must never
    # fall back to the old historical search label.
    if "annotation_values" in recheck:
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
    if not annotation_value.strip():
        raise ValueError("annotation_value is required for a recheck request")

    path, record = _load_json_record(json_path)
    if assignment_state_for_record(record) == ASSIGNMENT_SUPERSEDED:
        raise ValueError("Cannot request a recheck for a superseded silhouette")
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


def _normalise_annotation_values(values: list[str]) -> list[str]:
    """Return ordered, non-empty source annotation values without joining them."""
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value and value not in result:
            result.append(value)
    if not result:
        raise ValueError("At least one source annotation value is required")
    return result


def _recheck_annotation_values(recheck: dict[str, Any]) -> list[str]:
    raw_values = recheck.get("annotation_values")
    if isinstance(raw_values, list):
        return _normalise_annotation_values(raw_values)
    return []


def queue_recheck_pending(
    json_path: str | Path,
    *,
    annotation_values: list[str],
    reason: str = "canonical_search_recheck",
) -> dict[str, Any]:
    """Make one object inactive and queue exact current source values for recheck."""
    path, record = _load_json_record(json_path)
    if assignment_state_for_record(record) == ASSIGNMENT_SUPERSEDED:
        raise ValueError("Cannot request a recheck for a superseded silhouette")

    identity = extraction_identity(record)
    if not all(identity.values()):
        raise ValueError("Catalog record does not contain a complete extraction identity")
    values = _normalise_annotation_values(annotation_values)
    assignment = dict(record.get(ASSIGNMENT_FIELD) or {})
    existing = _pending_recheck(record)
    requested_at = (
        existing.get("requested_at")
        if isinstance(existing, dict) and existing.get("state") == RECHECK_PENDING
        else _now()
    )
    recheck = {
        "state": RECHECK_PENDING,
        "requested_at": requested_at,
        "annotation_values": values,
        **identity,
    }
    if len(values) == 1:
        recheck["annotation_value"] = values[0]
    assignment.update({
        "state": ASSIGNMENT_INACTIVE,
        "reason": reason,
        RECHECK_FIELD: recheck,
    })
    record[ASSIGNMENT_FIELD] = assignment
    _persist_record(path, record)
    return record


def pending_recheck_allows_annotation_value(
    record: dict[str, Any],
    *,
    media_type: str,
    media_id: str,
    shot_id: str,
    field: str,
    annotation_value: str,
) -> bool:
    """Return whether a pending recheck authorizes one exact atomic source value."""
    recheck = _pending_recheck(record)
    if assignment_state_for_record(record) != ASSIGNMENT_INACTIVE:
        return False
    if not recheck or recheck.get("state") != RECHECK_PENDING:
        return False
    identity = extraction_identity(record)
    if not all(identity.values()) or not all(
        str(recheck.get(key) or "") == value for key, value in identity.items()
    ):
        return False
    if (
        identity["media_type"] != media_type
        or identity["media_id"] != media_id
        or identity["shot_id"] != shot_id
        or identity["field"] != field
    ):
        return False
    try:
        return annotation_value in _recheck_annotation_values(recheck)
    except ValueError:
        return False


def mark_recheck_error(
    json_path: str | Path,
    *,
    error: str,
    annotation_values: list[str] | None = None,
) -> dict[str, Any]:
    """Persist an explicit failed recheck without deleting historical evidence."""
    path, record = _load_json_record(json_path)
    if assignment_state_for_record(record) == ASSIGNMENT_SUPERSEDED:
        raise ValueError("Cannot mark a superseded silhouette recheck as failed")
    identity = extraction_identity(record)
    if not all(identity.values()):
        raise ValueError("Catalog record does not contain a complete extraction identity")
    existing = _pending_recheck(record)
    values = annotation_values
    if values is None and isinstance(existing, dict):
        try:
            values = _recheck_annotation_values(existing)
        except ValueError:
            values = []
    values = _normalise_annotation_values(values or ["(unavailable)"])
    recheck = {
        "state": RECHECK_ERROR,
        "requested_at": existing.get("requested_at") if isinstance(existing, dict) else _now(),
        "failed_at": _now(),
        "error": str(error).strip()[:500] or "Unknown recheck failure",
        "annotation_values": values,
        **identity,
    }
    if len(values) == 1:
        recheck["annotation_value"] = values[0]
    assignment = dict(record.get(ASSIGNMENT_FIELD) or {})
    assignment.update({
        "state": ASSIGNMENT_INACTIVE,
        "reason": "canonical_search_recheck_error",
        RECHECK_FIELD: recheck,
    })
    record[ASSIGNMENT_FIELD] = assignment
    _persist_record(path, record)
    return record


def _normalise_source_job(source_job: dict[str, str]) -> dict[str, str]:
    """Validate the historical-label-free key of one source recheck job."""
    keys = ("media_type", "media_id", "shot_id", "field")
    result = {key: str(source_job.get(key) or "") for key in keys}
    if not all(result.values()):
        raise ValueError("Source recheck job requires media type, media ID, shot ID, and field")
    return result


def stage_recheck_replacements(
    *,
    source_job: dict[str, str],
    replacement_json_paths: list[str | Path],
) -> list[dict[str, Any]]:
    """Hide freshly extracted replacements until their whole source job succeeds."""
    source_job = _normalise_source_job(source_job)
    staged: list[dict[str, Any]] = []
    for raw_path in replacement_json_paths:
        path, record = _load_json_record(raw_path)
        if any(str(record.get(key) or "") != value for key, value in source_job.items()):
            raise ValueError("Replacement does not match the staged source recheck job")
        if assignment_state_for_record(record) == ASSIGNMENT_SUPERSEDED:
            raise ValueError("Cannot stage a superseded replacement")
        assignment = dict(record.get(ASSIGNMENT_FIELD) or {})
        assignment.update({
            "state": ASSIGNMENT_INACTIVE,
            "reason": SOURCE_RECHECK_STAGED,
            SOURCE_RECHECK_JOB_FIELD: source_job,
        })
        record[ASSIGNMENT_FIELD] = assignment
        _persist_record(path, record)
        staged.append(record)
    return staged


def staged_recheck_replacement_matches(
    record: dict[str, Any],
    *,
    source_job: dict[str, str],
    annotation_value: str,
) -> bool:
    """Return whether an inactive object is a reusable staged source result."""
    source_job = _normalise_source_job(source_job)
    assignment = record.get(ASSIGNMENT_FIELD)
    return bool(
        assignment_state_for_record(record) == ASSIGNMENT_INACTIVE
        and isinstance(assignment, dict)
        and assignment.get("reason") == SOURCE_RECHECK_STAGED
        and assignment.get(SOURCE_RECHECK_JOB_FIELD) == source_job
        and str(record.get("label") or "") == annotation_value
        and all(str(record.get(key) or "") == value for key, value in source_job.items())
    )


def complete_recheck_source(
    project_path: str | Path,
    *,
    old_json_paths: list[str | Path],
    replacement_json_paths: list[str | Path],
    annotation_values: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Complete one source recheck for every linked historical object.

    Historical labels are deliberately excluded from the source-job identity:
    all records must agree only on media type, media ID, source shot, and
    field. Each JSON is written atomically; cross-file lifecycle updates are a
    recoverable sequence because no filesystem-wide transaction exists.
    """
    old_paths = [Path(path) for path in old_json_paths]
    if not old_paths:
        raise ValueError("At least one historical silhouette is required")
    if len({str(path.resolve()) for path in old_paths}) != len(old_paths):
        raise ValueError("Historical source job contains duplicate object paths")

    historical: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    source_identity: dict[str, str] | None = None
    for old_path in old_paths:
        path, old_record = _load_json_record(old_path)
        old_recheck = _pending_recheck(old_record)
        if assignment_state_for_record(old_record) != ASSIGNMENT_INACTIVE or not old_recheck:
            raise ValueError("Historical silhouette is not an inactive recheck request")
        if old_recheck.get("state") != RECHECK_PENDING:
            raise ValueError("Historical recheck request is not pending")
        identity = extraction_identity(old_record)
        if not all(identity.values()):
            raise ValueError("Historical catalog record lacks a complete extraction identity")
        base_identity = {
            key: identity[key]
            for key in ("media_type", "media_id", "shot_id", "field")
        }
        if source_identity is None:
            source_identity = base_identity
        elif base_identity != source_identity:
            raise ValueError("Historical silhouettes do not belong to one source recheck job")
        historical.append((path, old_record, old_recheck))

    source_values = _normalise_annotation_values(annotation_values)
    assert source_identity is not None

    paths = [Path(path) for path in replacement_json_paths]
    if len({str(path.resolve()) for path in paths}) != len(paths):
        raise ValueError("Replacement list contains duplicate object paths")
    replacements: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        if path in old_paths:
            raise ValueError("Replacement must be distinct from the historical object")
        replacement_path, replacement = _load_json_record(path)
        if any(
            str(replacement.get(key) or "") != source_identity[key]
            for key in ("media_type", "media_id", "shot_id", "field")
        ):
            raise ValueError("Replacement does not match the source media, shot, and field")
        if str(replacement.get("label") or "") not in source_values:
            raise ValueError("Replacement does not contain a current source annotation value")
        replacements.append((replacement_path, replacement))

    return _commit_completed_source_job(
        historical=historical,
        replacements=replacements,
        source_identity=source_identity,
        source_values=source_values,
    )


def _commit_completed_source_job(
    *,
    historical: list[tuple[Path, dict[str, Any], dict[str, Any]]],
    replacements: list[tuple[Path, dict[str, Any]]],
    source_identity: dict[str, str],
    source_values: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Persist one fully prepared source job without an interleaved SIGINT."""
    references: list[dict[str, str]] = []
    updated_replacements: list[dict[str, Any]] = []
    updated_historical: list[dict[str, Any]] = []
    try:
        with _defer_sigint_during_source_commit():
            for replacement_path, replacement in replacements:
                reference = catalog_object_reference(replacement, replacement_path)
                assignment = active_assignment()
                previous = replacement.get(ASSIGNMENT_FIELD)
                previous_refs = previous.get("supersedes") if isinstance(previous, dict) else None
                if isinstance(previous_refs, list):
                    supersedes = list(previous_refs)
                elif isinstance(previous_refs, dict):
                    supersedes = [previous_refs]
                else:
                    supersedes = []
                for old_path, old_record, _old_recheck in historical:
                    old_reference = catalog_object_reference(old_record, old_path)
                    if old_reference not in supersedes:
                        supersedes.append(old_reference)
                assignment["supersedes"] = supersedes[0] if len(supersedes) == 1 else supersedes
                assignment.pop("reason", None)
                assignment.pop(SOURCE_RECHECK_JOB_FIELD, None)
                replacement[ASSIGNMENT_FIELD] = assignment
                if not isinstance(replacement.get("search_provenance"), dict):
                    replacement["search_provenance"] = {
                        "state": "valid",
                        "method": "recheck_source_annotation",
                        "rechecked_at": _now(),
                        "annotation_values": [str(replacement.get("label"))],
                    }
                _persist_record(replacement_path, replacement)
                references.append(reference)
                updated_replacements.append(replacement)

            for old_path, old_record, old_recheck in historical:
                completed_recheck = dict(old_recheck)
                completed_recheck.update({
                    "state": RECHECK_COMPLETED,
                    "completed_at": _now(),
                    "annotation_values": source_values,
                    "source_job": source_identity,
                    "result": "objects" if references else "no_objects",
                    "replacement_objects": references,
                })
                old_assignment = dict(old_record.get(ASSIGNMENT_FIELD) or {})
                old_assignment.update({
                    "state": ASSIGNMENT_SUPERSEDED,
                    "superseded_by": references,
                    RECHECK_FIELD: completed_recheck,
                })
                old_assignment.pop("reason", None)
                old_record[ASSIGNMENT_FIELD] = old_assignment
                _persist_record(old_path, old_record)
                updated_historical.append(old_record)
    except KeyboardInterrupt as exc:
        raise SourceJobCommitInterrupted(
            updated_historical, updated_replacements,
        ) from exc
    return updated_historical, updated_replacements


def complete_recheck_replacements(
    project_path: str | Path,
    *,
    old_json_path: str | Path,
    replacement_json_paths: list[str | Path],
    annotation_values: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Backward-compatible single-record wrapper around source-job completion."""
    historical, replacements = complete_recheck_source(
        project_path,
        old_json_paths=[old_json_path],
        replacement_json_paths=replacement_json_paths,
        annotation_values=annotation_values,
    )
    return historical[0], replacements


def _annotation_value_for_record(project_path: str | Path, record: dict[str, Any]) -> str:
    """Return one unambiguous source-shot annotation value for *record*."""
    from data.annotate import get_annotation_json_path

    filename = str(record.get("filename") or "")
    media_type = str(record.get("media_type") or "")
    shot_id = str(record.get("shot_id") or "")
    field = str(record.get("field") or "")
    if not all((filename, media_type, shot_id, field)):
        raise ValueError("Catalog record cannot resolve its source annotation")

    annotation_path = get_annotation_json_path(str(project_path), filename, media_type)
    try:
        entries = json.loads(annotation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read source annotation: {annotation_path}") from exc

    annotation: dict[str, Any] | None = None
    if isinstance(entries, list):
        for entry in entries:
            shot = entry.get("shot") if isinstance(entry, dict) else None
            if isinstance(shot, dict) and str(shot.get("shot_id") or "") == shot_id:
                candidate = shot.get("annotation")
                annotation = candidate if isinstance(candidate, dict) else None
                break
    if annotation is None:
        raise ValueError(f"Source shot is not annotated: {shot_id}")

    raw_values = annotation.get(field)
    values = [
        value.strip()
        for value in (raw_values if isinstance(raw_values, list) else [raw_values])
        if isinstance(value, str) and value.strip()
    ]
    if not values:
        raise ValueError(f"Source shot has no usable annotation for field '{field}'")

    provenance = record.get("search_provenance")
    provenance_values = (
        provenance.get("annotation_values")
        if isinstance(provenance, dict) else None
    )
    candidates = [
        value for value in values
        if isinstance(provenance_values, list) and value in provenance_values
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(values) == 1:
        return values[0]
    raise ValueError(
        f"Source annotation for field '{field}' is ambiguous; cannot queue a recheck safely"
    )


def deassign_catalog_object(
    project_path: str | Path,
    *,
    media_type: str,
    filename_stem: str,
    media_id: str,
    shot_id: str,
    field: str,
    label: str,
    object_id: str,
) -> dict[str, Any]:
    """De-assign one exact catalog object and rebuild its scoped browse index.

    All catalog identity fields are required so a stale curator client cannot
    target a same-labeled object from another source shot. Historical PNG/JSON
    assets, search provenance, and human-best selection remain untouched.
    """
    request = {
        "media_type": str(media_type or ""),
        "filename_stem": str(filename_stem or ""),
        "media_id": str(media_id or ""),
        "shot_id": str(shot_id or ""),
        "field": str(field or ""),
        "label": str(label or ""),
        "object_id": str(object_id or ""),
    }
    if not all(request.values()):
        raise ValueError("Complete catalog object identity is required for de-assignment")

    matches: list[dict[str, Any]] = []
    for record in iter_catalog(
        str(project_path),
        media_type=request["media_type"],
        filename_stem=request["filename_stem"],
        label=request["label"],
    ):
        if "error" in record:
            continue
        reference = catalog_object_reference(record, record.get("path"))
        if (
            str(record.get("media_type") or "") == request["media_type"]
            and str(record.get("filename_stem") or "") == request["filename_stem"]
            and str(record.get("media_id") or "") == request["media_id"]
            and str(record.get("shot_id") or "") == request["shot_id"]
            and str(record.get("field") or "") == request["field"]
            and str(record.get("label") or "") == request["label"]
            and str(reference.get("object_id") or "") == request["object_id"]
        ):
            matches.append(record)

    if not matches:
        raise ValueError("No catalog object matches the requested identity")
    if len(matches) != 1:
        raise ValueError("Catalog object identity is ambiguous; refusing de-assignment")

    record = matches[0]
    path = Path(record["path"])
    identity = extraction_identity(record)
    if assignment_state_for_record(record) == ASSIGNMENT_INACTIVE:
        if pending_recheck_matches(record, **identity):
            return {"status": "already_inactive", "record": record, "index": None}
        raise ValueError("Catalog object is inactive without a matching pending recheck")
    if assignment_state_for_record(record) == ASSIGNMENT_SUPERSEDED:
        raise ValueError("Cannot de-assign a superseded silhouette")

    annotation_value = _annotation_value_for_record(project_path, record)
    updated = mark_recheck_pending(path, annotation_value=annotation_value)
    from services.illustration_index import rebuild_index

    index = rebuild_index(project_path, "silhouettes", request["media_type"])
    if index.get("status") != "ready":
        raise RuntimeError("Silhouette was de-assigned but the Illustration index did not rebuild")
    return {"status": "deassigned", "record": updated, "index": index}


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
