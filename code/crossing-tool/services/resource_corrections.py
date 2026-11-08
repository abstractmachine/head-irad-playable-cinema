"""Approved missing-field corrections, distinct from reads and publication.

Proposals bind exact historical bytes and one source annotation value. Application
is journaled before the atomic metadata write and resumable after interruption.
It does not allocate an instance, publish files, change assignment or rebuild an index.
"""

from __future__ import annotations

import hashlib
import json

from data import resource_locations as locations
from data.media_id import label_slug, parse_shot_id
from data.resource_storage import ResourceStore, digest, relative_path, registry_lock
from services.resource_validation import source_annotation


def _token(proposal):
    payload = {key: value for key, value in proposal.items() if key != "approval_token"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def propose_missing_field(project, reference, field, annotation_value, *, annotation_layout="legacy-v1"):
    """No guessing: the caller supplies a category and exact source annotation value."""
    if relative_path(project, reference) != str(reference) or not str(reference).startswith("data/silhouettes/catalog/"):
        raise ValueError("An exact project-relative historical silhouette reference is required")
    locations.component(field)
    path = locations.project_file(project, reference)
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or record.get("field") not in (None, ""):
        raise ValueError("Correction only applies to an unresolved missing field")
    media_id = parse_shot_id(record["shot_id"])[0]
    media = locations.find_media(project, record["media_type"], media_id=media_id, filename=record["filename"])
    annotation_path, annotation = source_annotation(project, media["filename"], record["media_type"], record["shot_id"], layout=annotation_layout)
    values = annotation.get(field)
    values = values if isinstance(values, list) else [values]
    if (not isinstance(annotation_value, str) or not annotation_value.strip() or annotation_value not in values
            or not isinstance(record.get("label"), str) or label_slug(annotation_value) != label_slug(record["label"])):
        raise ValueError("Proposed category/value is not consistent with the exact source annotation and label")
    proposal = {"reference": reference, "before_sha256": digest(path), "before": record, "field": field,
                "annotation_value": annotation_value, "annotation_reference": relative_path(project, annotation_path),
                "annotation_sha256": digest(annotation_path), "annotation_layout": annotation_layout}
    proposal["approval_token"] = _token(proposal)
    return proposal


def apply_missing_field(project, registry, proposal, *, approval, approved=False, interrupt=None):
    """Apply only the approved proposal; interrupted identical approval is retryable."""
    from data.annotate import atomic_write_text
    from services.illustration_index import invalidate_for_record
    store = ResourceStore(project, registry, approved=approved)
    if not isinstance(approval, str) or approval != _token(proposal) or proposal.get("approval_token") != approval:
        raise ValueError("Approval does not match the exact correction proposal")
    reference = relative_path(project, proposal["reference"])
    path = locations.project_file(project, reference)
    annotation = locations.project_file(project, proposal["annotation_reference"])
    after = {**proposal["before"], "field": proposal["field"]}
    after_text = json.dumps(after, sort_keys=True, allow_nan=False)
    after_sha = hashlib.sha256(after_text.encode()).hexdigest()
    with store.transaction() as database:
        if not database.execute("SELECT 1 FROM adoption_exceptions WHERE reference=? AND reason='missing field'", (reference,)).fetchone():
            raise ValueError("Correction is not an explicitly adopted unresolved exception")
        if digest(annotation) != proposal["annotation_sha256"]:
            raise ValueError("Source annotation changed after correction proposal")
        existing = database.execute("SELECT details FROM classification_corrections WHERE correction_id=?", (approval,)).fetchone()
        current_sha = digest(path)
        if current_sha not in (proposal["before_sha256"], after_sha):
            raise ValueError("Historical metadata changed after correction proposal")
        if existing is None:
            confirmed = propose_missing_field(project, reference, proposal["field"], proposal["annotation_value"],
                                               annotation_layout=proposal["annotation_layout"])
            if confirmed != proposal:
                raise ValueError("Correction proposal is stale")
            details = {"state": "prepared", "proposal": proposal, "after_sha256": after_sha}
            database.execute("INSERT INTO classification_corrections VALUES(?,?,?,?,?,?)",
                             (approval, reference, proposal["before_sha256"], proposal["field"], approval, json.dumps(details)))
            database.commit()
        else:
            details = json.loads(existing[0])
            if details["state"] == "applied":
                if current_sha != after_sha:
                    raise ValueError("Applied correction metadata no longer matches")
                return {"correction_id": approval, "state": "applied", "changed": False}
        if interrupt:
            interrupt("prepared")
        if current_sha == proposal["before_sha256"]:
            atomic_write_text(path, after_text)
        if interrupt:
            interrupt("metadata-written")
        if digest(path) != after_sha:
            raise ValueError("Corrected metadata checksum mismatch")
        if not invalidate_for_record(path, "silhouettes"):
            raise ValueError("Corrected historical record cannot invalidate its canonical derived index")
        details["state"] = "applied"
        database.execute("UPDATE classification_corrections SET details=? WHERE correction_id=?", (json.dumps(details), approval))
        database.commit()
        return {"correction_id": approval, "state": "applied", "changed": True}


def correction_status(project, registry, reference):
    """Read-only explicit operational state; never finish an interrupted correction."""
    import sqlite3
    path = locations.project_file(project, registry)
    with registry_lock(project, registry), sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        rows = database.execute("SELECT correction_id,details FROM classification_corrections WHERE reference=? ORDER BY correction_id", (reference,))
        return [{"correction_id": row[0], **json.loads(row[1])} for row in rows]