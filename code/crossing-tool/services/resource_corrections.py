"""Approved missing-field corrections, distinct from reads and publication.

Proposals bind exact historical bytes and one source annotation value. Application
is journaled before the atomic metadata write and resumable after interruption.
It does not allocate an instance, publish files, change assignment or rebuild an index.
"""

from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path

from data import resource_locations as locations
from data.media_id import label_slug, parse_shot_id
from data.resource_storage import ResourceStore, digest, relative_path, registry_lock
from services.resource_validation import source_annotation


def propose_objects_addition(project, reference, shot_id, *, expected_sha256, value):
    """Prepare one exact additive source edit for controlled collection publication."""
    from services.resource_publication import _validate_collection_payload
    path = locations.project_file(project, reference)
    if digest(path) != expected_sha256:
        raise ValueError("Frozen source annotation changed before proposal")
    before = json.loads(path.read_text())
    after = copy.deepcopy(before)
    matches = [entry["shot"] for entry in after if entry.get("shot", {}).get("shot_id") == shot_id]
    if len(matches) != 1 or not isinstance(value, str) or not value.strip():
        raise ValueError("Exact source shot and atomic object value are required")
    objects = matches[0].get("annotation", {}).get("objects")
    if not isinstance(objects, list) or value in objects:
        raise ValueError("Additive correction requires an existing objects array and a new value")
    objects.append(value)
    media_id = parse_shot_id(shot_id)[0]
    media = locations.find_media(project, "movie", media_id=media_id)
    if locations.annotation_path(project, media["filename"], "movie") != path:
        raise ValueError("Annotation path differs from exact source media")
    payload = json.dumps(after, indent=2, ensure_ascii=False, allow_nan=False).encode()
    _validate_collection_payload(project, media, {".annotations.json": path},
                                 {".annotations.json": payload}, {".annotations.json"}, media_type="movie")
    return {"reference": str(reference), "shot_id": shot_id, "media": media,
            "before_sha256": expected_sha256, "after_sha256": hashlib.sha256(payload).hexdigest(),
            "payload": payload, "value": value}


def _token(proposal):
    payload = {key: value for key, value in proposal.items() if key != "approval_token"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def plan_registered_reclassification(project, registry, instance_id, field, label):
    from services.resource_resolution import resolve_silhouette
    from services.resource_validation import validate_source_classification
    current = resolve_silhouette(project, instance_id, registry=registry, active_only=False)
    binding = locations.registered_instance(project, registry, instance_id)
    record = current["record"]
    commitment = locations.annotation_commitment(project, record["filename"], record["media_type"], registry)
    locations.resolve_annotation(project, record["filename"], record["media_type"], registry=registry)
    if commitment is None:
        raise ValueError("Reclassification planning requires committed source annotations")
    findings = validate_source_classification(project, {**record, "field": field, "label": label, "source_annotation_value": label}, annotation_layout=commitment["layout"])
    if findings["state"] != "valid":
        raise ValueError("Reclassification source/classification invalid: " + json.dumps(findings))
    media = locations.find_media(project, record["media_type"], media_id=record["media_id"])
    pairs = []
    for row in binding["locations"]:
        original = locations.current_location(project, binding, row["kind"], row["mode"])
        target = locations.silhouette_destination(project, media, binding["media_type"], instance_id, field, label) if row["kind"] == "silhouettes" else locations.engraving_destination(project, media, binding["media_type"], instance_id, field, label, row["mode"])
        for key in ("metadata", "image"):
            if target[key] != original[key] and target[key].exists():
                raise ValueError("Reclassification destination already exists")
        pairs.append({"kind": row["kind"], "mode": row["mode"], "revision": row["revision"],
                      "source": {key: str(original[key]) for key in ("metadata", "image")},
                      "destination": {key: str(target[key]) for key in ("metadata", "image")}})
    runs = []
    for run in binding["runs"]:
        if run["state"] in ("reserved", "prepared"):
            raise ValueError("Reclassification requires explicit recovery of in-progress generation attempts")
        target = locations.generation_run_destination(project, media, binding["media_type"], instance_id, field, label, run["provider"], run["number"]) / "run.json"
        if str(target.relative_to(Path(project).resolve())) != run["run_path"] and target.exists():
            raise ValueError("Reclassification run destination already exists")
        runs.append({"provider": run["provider"], "number": run["number"], "state": run["state"], "source": run["run_path"], "destination": str(target), "sha256": run["run_sha256"]})
    return {"instance_id": instance_id, "source_annotation_revision": commitment["revision"], "field": field, "label": label,
            "pairs": pairs, "runs": runs, "execution_supported": False,
            "limitation": "Coordinated pair/run-tree relocation and registry CAS are not implemented; preserve all bytes, IDs, modes, review/assignment state and run ordinals. No partial move is authorized."}


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


def execute_classification_cohort(project, registry, cases, additions, *, operation_id, approved=False, interrupt=None):
    """Recover exactly one approved additive annotation / missing-field cohort."""
    from data.resource_storage import create_storage_directory
    from services.resource_publication import _copy, _sync, publish_annotation_collection, resume_annotation_collection
    store = ResourceStore(project, registry, approved=approved)
    if not isinstance(operation_id, str) or not operation_id.isalnum() or len(cases) != 6 or len(additions) != 2:
        raise ValueError("Exact six-case/two-annotation cohort required")
    annotations = [{key: value for key, value in item.items() if key != "payload"} for item in additions]
    with store.transaction() as database:
        existing = database.execute("SELECT * FROM publication_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if existing is None:
            if database.execute("SELECT 1 FROM resource_locations LIMIT 1").fetchone():
                raise ValueError("Historical correction cohort requires an unpublished preparation registry")
            if database.execute("SELECT 1 FROM publication_operations WHERE state NOT IN ('finished','rolled_back')").fetchone():
                raise ValueError("Recover pending publication before preparing a correction cohort")
            for item in cases:
                path = locations.project_file(project, item["reference"])
                record = json.loads(path.read_text())
                if digest(path) != item["before_sha256"] or digest(path.with_suffix(".png")) != item["image_sha256"]:
                    raise ValueError("Frozen correction pair changed")
                if record.get("field") not in (None, "") or any(record[key] != item[key] for key in ("media_id", "shot_id", "frame", "label")):
                    raise ValueError("Frozen correction source association differs")
                if digest(locations.project_file(project, item["annotation_reference"])) != item["annotation_sha256"]:
                    raise ValueError("Frozen correction annotation changed")
            for item in additions:
                if digest(locations.project_file(project, item["reference"])) != item["before_sha256"] or hashlib.sha256(item["payload"]).hexdigest() != item["after_sha256"]:
                    raise ValueError("Frozen annotation addition differs")
            stage = store.registry.parent / ".resource-publications" / operation_id
            create_storage_directory(project, stage, exclusive=True)
            for number, item in enumerate(cases):
                _copy(locations.project_file(project, item["reference"]), stage / ("previous-case-" + str(number) + ".json"))
            for number, item in enumerate(additions):
                _copy(locations.project_file(project, item["reference"]), stage / ("previous-annotation-" + str(number) + ".json"))
                (stage / ("new-annotation-" + str(number) + ".json")).write_bytes(item["payload"])
                _sync(stage / ("new-annotation-" + str(number) + ".json"))
            _sync(stage)
            details = {"cases": cases, "annotations": annotations, "stage": relative_path(project, stage)}
            database.execute("INSERT INTO publication_operations VALUES(?,NULL,'classification-cohort','','prepared',?)", (operation_id, json.dumps(details)))
        else:
            details = json.loads(existing["details"])
            if existing["kind"] != "classification-cohort" or details["cases"] != cases or details["annotations"] != annotations:
                raise ValueError("Cohort approval differs from retained intent")
            if existing["state"] == "rolled_back":
                raise ValueError("Rolled-back cohort cannot be silently resumed")
    if interrupt:
        interrupt("cohort-prepared")
    with registry_lock(project, registry, exclusive=True):
        with store.transaction() as database:
            database.execute("UPDATE publication_operations SET state='installing' WHERE operation_id=? AND state='prepared'", (operation_id,))
        for number, item in enumerate(annotations):
            child = operation_id + "annotation" + str(number)
            with store.transaction() as database:
                previous = database.execute("SELECT state FROM publication_operations WHERE operation_id=?", (child,)).fetchone()
                commitment = database.execute("SELECT * FROM annotation_layouts WHERE media_type=? AND media_id=?", (item["media_type"], item["media_id"])).fetchone()
            if previous is not None:
                resume_annotation_collection(project, registry, child, approved=approved,
                                             interrupt=(lambda state: interrupt("annotation" + str(number) + ":" + state)) if interrupt else None)
            else:
                payload = locations.project_file(project, Path(project) / details["stage"] / ("new-annotation-" + str(number) + ".json")).read_bytes()
                publish_annotation_collection(project, registry, item["filename"], item["media_type"], {".annotations.json": payload},
                                              expected_revision=commitment["revision"], expected_hashes=json.loads(commitment["artifacts"]),
                                              approved=approved, operation_id=child, parent_operation=operation_id,
                                              interrupt=(lambda state: interrupt("annotation" + str(number) + ":" + state)) if interrupt else None)
        for number, item in enumerate(cases):
            with store.transaction() as database:
                retained = database.execute("SELECT details FROM classification_corrections WHERE reference=?", (item["reference"],)).fetchone()
            proposal = json.loads(retained[0])["proposal"] if retained else propose_missing_field(project, item["reference"], item["approved_field"], item["label"])
            if item.get("approval_token") and proposal["approval_token"] != item["approval_token"]:
                raise ValueError("Existing human approval token differs")
            apply_missing_field(project, registry, proposal, approval=proposal["approval_token"], approved=approved,
                                interrupt=(lambda state: interrupt("case" + str(number) + ":" + state)) if interrupt else None)
            path = locations.project_file(project, item["reference"])
            before = json.loads(locations.project_file(project, Path(project) / details["stage"] / ("previous-case-" + str(number) + ".json")).read_text())
            if json.loads(path.read_text()) != {**before, "field": item["approved_field"]} or digest(path.with_suffix(".png")) != item["image_sha256"]:
                raise ValueError("Correction changed unapproved metadata or PNG bytes")
        with store.transaction() as database:
            database.execute("UPDATE publication_operations SET state='finished' WHERE operation_id=?", (operation_id,))
        return {"operation_id": operation_id, "state": "finished", "corrections": 6, "annotation_updates": 2}