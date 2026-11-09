"""Deterministic read-only publication validation, separate from repair/lookup."""

from __future__ import annotations

import json
from pathlib import Path

from data import resource_locations as locations
from data.resource_storage import digest
from data.media_id import parse_shot_id, label_slug

CONTRACT = "media-first-v1"


def validate_generation(generation, provider):
    """Shared provider-aware reproduction check for runs and publications."""
    missing = []
    errors = []
    if not isinstance(generation, dict):
        return {"missing": ["generation"], "errors": ["generation must be an object"]}
    for key in ("service", "model", "prompt", "parameters", "software"):
        if key not in generation or generation[key] is None or generation[key] == "":
            missing.append(key)
    for key in ("service", "model", "prompt"):
        if key in generation and (not isinstance(generation[key], str) or not generation[key].strip()):
            errors.append("invalid generation " + key)
    if not isinstance(generation.get("parameters"), dict):
        errors.append("generation parameters must be explicit")
    software = generation.get("software")
    if software is not None and (not isinstance(software, dict) or not software
                                 or any(not isinstance(value, str) or not value.strip() for value in software.values())):
        errors.append("generation software versions must be explicit")
    if provider == "local" and (type(generation.get("seed")) is not int or generation["seed"] < 0):
        missing.append("local generation seed")
    elif provider == "openai" and generation.get("seed_policy") != "provider-managed":
        missing.append("explicit OpenAI provider-managed seed policy")
    elif provider not in locations.PROVIDERS:
        errors.append("unsupported generation provider")
    if not generation.get("model_version") and generation.get("version_policy") != "provider-managed":
        missing.append("model version or explicit provider-managed version policy")
    return {"missing": missing, "errors": errors}


def source_annotation(project, filename, media_type, shot_id, *, layout="legacy-v1"):
    """Resolve one exact source annotation, shared by validation and correction."""
    path = locations.project_file(project, locations.annotation_path(project, filename, media_type, layout=layout))
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError("Source annotations must be a shot list")
    matches = [entry["shot"] for entry in entries if isinstance(entry, dict)
               and isinstance(entry.get("shot"), dict) and entry["shot"].get("shot_id") == shot_id]
    if len(matches) != 1 or not isinstance(matches[0].get("annotation"), dict):
        raise ValueError("Ambiguous, missing or stale source annotation")
    return path, matches[0]["annotation"]


def validate_source_classification(project, metadata, *, annotation_layout="legacy-v1", segmentation=False):
    """Shared legacy/canonical publication gate, independent of instance allocation."""
    missing = [key for key in ("media_type", "media_id", "filename", "shot_id", "field", "label")
               if metadata.get(key) is None or metadata.get(key) == ""]
    errors = []
    if missing:
        return {"state": "incomplete", "missing": missing, "errors": errors}
    try:
        locations.component(metadata["field"])
        if not isinstance(metadata["label"], str) or not metadata["label"].strip():
            raise ValueError("invalid explicit label")
        media, start, end = parse_shot_id(metadata["shot_id"])
        if media != metadata["media_id"]:
            raise ValueError("source shot/media mismatch")
        locations.find_media(project, metadata["media_type"], media_id=media, filename=metadata["filename"])
        dirty = metadata.get("source_validation", {})
        current_shot = dirty.get("current_shot_id", metadata["shot_id"]) if dirty.get("dirty") else metadata["shot_id"]
        _, annotation = source_annotation(project, metadata["filename"], metadata["media_type"], current_shot, layout=annotation_layout)
        values = annotation.get(metadata["field"])
        values = values if isinstance(values, list) else [values]
        association = metadata.get("source_annotation_value", metadata["label"])
        from services.search import _score_annotation_value
        if (not isinstance(association, str) or not association.strip()
            or _score_annotation_value(association, values) <= 0
            or _score_annotation_value(metadata["label"], association) <= 0):
            errors.append("category/label source annotation association mismatch")
        if "frame" in metadata and (type(metadata["frame"]) is not int or not start <= metadata["frame"] <= end):
            errors.append("invalid/stale source frame")
    except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
        errors.append("source classification unavailable or contradictory: " + str(exc))
    if segmentation:
        for key in ("frame", "bbox", "frame_size", "mask_area", "sam_model", "frame_match_model", "source_frame"):
            if metadata.get(key) is None or metadata.get(key) == "":
                missing.append(key)
        bbox, size, area = metadata.get("bbox"), metadata.get("frame_size"), metadata.get("mask_area")
        if not isinstance(bbox, list) or len(bbox) != 4 or any(type(value) is not int for value in bbox):
            errors.append("invalid segmentation bbox")
        elif not isinstance(size, list) or len(size) != 2 or any(type(value) is not int or value < 1 for value in size):
            errors.append("invalid source frame dimensions")
        else:
            left, top, width, height = bbox
            if left < 0 or top < 0 or width < 1 or height < 1 or left + width > size[0] or top + height > size[1]:
                errors.append("segmentation bbox exceeds source frame")
            if type(area) is not int or area < 1 or area > width * height:
                errors.append("invalid mask area")
    return {"state": "invalid" if errors else "incomplete" if missing else "valid", "missing": missing, "errors": errors}


def validate_annotation_classifications(annotation):
    """Validate explicit classified entries without imposing object geometry on text."""
    findings = []
    def visit(value, path):
        if isinstance(value, dict):
            if "field" in value or "label" in value:
                field, label = value.get("field"), value.get("label")
                if not isinstance(field, str) or not field.strip():
                    findings.append({"path": path, "state": "incomplete", "message": "explicit field required"})
                elif not isinstance(label, str) or not label.strip():
                    findings.append({"path": path, "state": "incomplete", "message": "explicit label required"})
                else:
                    raw = annotation.get(field)
                    raw = raw if isinstance(raw, list) else [raw]
                    labels = [item if isinstance(item, str) else item.get("label") if isinstance(item, dict) and item.get("field") == field else None for item in raw]
                    from services.search import _score_annotation_value
                    if _score_annotation_value(label, labels) <= 0:
                        findings.append({"path": path, "state": "invalid", "message": "classification differs from source field vocabulary"})
            for key, child in value.items():
                visit(child, path + "." + str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + "[" + str(index) + "]")
    if not isinstance(annotation, dict):
        return [{"path": "annotation", "state": "invalid", "message": "annotation must be an object"}]
    visit(annotation, "annotation")
    return findings


def validate_metadata(project, binding, metadata, kind, mode="", *, annotation_layout="legacy-v1", preserve_assignment=False):
    """Return valid/incomplete/invalid/historical and ordered explicit findings.

    Existing mask fields and actual annotation keys are used, not a replacement
    category ontology. Dirty state is retained and never cleared by validation.
    """
    missing = []
    errors = []
    if not isinstance(metadata, dict):
        return {"state": "invalid", "missing": [], "errors": ["metadata must be an object"], "dirty": False}
    historical = metadata.get("publication_contract") is None
    if not historical and metadata.get("publication_contract") != CONTRACT:
        errors.append("unknown publication contract")
    def required(key, container=metadata):
        if key not in container or container[key] is None or container[key] == "":
            missing.append(key)
            return None
        return container[key]
    identity_key = "instance_id" if kind == "silhouettes" else "source_instance_id"
    if required(identity_key) not in (None, binding["instance_id"]):
        errors.append("registered instance identity mismatch")
    if required("resource_kind") not in (None, kind):
        errors.append("resource kind mismatch")
    for key in ("media_id", "media_type", "shot_id"):
        if required(key) not in (None, binding[key]):
            errors.append(key + " differs from registered association")
    field = required("field")
    label = required("label")
    if field is not None:
        try:
            locations.component(field)
        except ValueError:
            errors.append("invalid annotation category")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        errors.append("invalid annotation label")
    validation = metadata.get("source_validation", {})
    if not isinstance(validation, dict) or ("dirty" in validation and type(validation["dirty"]) is not bool):
        errors.append("malformed source_validation")
        validation = {}
    dirty = validation.get("dirty", False)
    if validation:
        if (validation.get("instance_id", binding["instance_id"]) != binding["instance_id"]
                or validation.get("registered_shot_id", binding["shot_id"]) != binding["shot_id"]):
            errors.append("dirty assessment changes immutable identity")
        try:
            if parse_shot_id(validation.get("current_shot_id", binding["shot_id"]))[0] != binding["media_id"]:
                raise ValueError("different source media")
        except (ValueError, TypeError):
            errors.append("dirty assessment has invalid current source shot")
        if dirty and (not isinstance(validation.get("reasons"), list) or not validation["reasons"]
                      or any(not isinstance(reason, str) or not reason for reason in validation["reasons"])):
            errors.append("dirty assessment requires explicit reasons")
    try:
        record = locations.find_media(project, binding["media_type"], media_id=binding["media_id"])
        builder = locations.silhouette_destination if kind == "silhouettes" else locations.engraving_destination
        args = (project, record, binding["media_type"], binding["instance_id"], field, label)
        destination = builder(*args) if kind == "silhouettes" else builder(*args, mode)
        for key, expected in (("filename", record["filename"]), ("filename_stem", Path(record["filename"]).stem)):
            if required(key) not in (None, expected):
                errors.append(key + " differs from authoritative media")
    except (ValueError, TypeError, KeyError):
        destination = None
    if kind == "silhouettes":
        if mode != "":
            errors.append("silhouette mode must be empty")
        frame = required("frame")
        _, start, end = parse_shot_id(binding["shot_id"])
        if frame is not None and (type(frame) is not int or frame < 0 or not start <= frame <= end):
            errors.append("invalid/stale source frame")
        bbox = required("bbox")
        size = required("frame_size")
        area = required("mask_area")
        if bbox is not None and (not isinstance(bbox, list) or len(bbox) != 4 or any(type(value) is not int for value in bbox)):
            errors.append("invalid segmentation bbox")
        if size is not None and (not isinstance(size, list) or len(size) != 2 or any(type(value) is not int or value < 1 for value in size)):
            errors.append("invalid source frame dimensions")
        if area is not None and (type(area) is not int or area < 1):
            errors.append("invalid mask area")
        if (isinstance(bbox, list) and len(bbox) == 4 and all(type(value) is int for value in bbox)
                and isinstance(size, list) and len(size) == 2 and all(type(value) is int and value > 0 for value in size)):
            left, top, width, height = bbox
            if left < 0 or top < 0 or width < 1 or height < 1 or left + width > size[0] or top + height > size[1]:
                errors.append("segmentation bbox exceeds source frame")
            elif type(area) is int and area > width * height:
                errors.append("mask area exceeds segmentation bbox")
        for key in ("sam_model", "frame_match_model", "source_frame"):
            required(key)
        if field is not None and label is not None and destination is not None:
            classification = validate_source_classification(project, metadata, annotation_layout=annotation_layout)
            errors.extend(classification["errors"])
            missing.extend(key for key in classification["missing"] if key not in missing)
        assignment = metadata.get("assignment", {"state": "active"})
        if not isinstance(assignment, dict) or assignment.get("state") != "active":
            current = locations.current_location(project, binding, "silhouettes", "") if preserve_assignment else None
            if current is None or current["record"].get("assignment") != assignment:
                errors.append("only active validated silhouettes may be published")
    elif kind == "engravings":
        if mode not in locations.MODES or required("mode") != mode:
            errors.append("engraving mode mismatch")
        if metadata.get("status") != "generated":
            errors.append("engraving is not generated")
        generation = metadata.get("generation")
        source = metadata.get("source")
        run = metadata.get("published_run")
        for key, value in (("generation", generation), ("source", source), ("published_run", run)):
            if not isinstance(value, dict):
                missing.append(key)
        if isinstance(generation, dict) and isinstance(source, dict) and isinstance(run, dict):
            provider = run.get("provider")
            reproduction = validate_generation(generation, provider)
            missing.extend(reproduction["missing"])
            errors.extend(reproduction["errors"])
            if any(key in source for key in ("silhouette_json", "silhouette_png", "source_json")):
                errors.append("canonical source relationship must not contain obsolete paths")
            try:
                silhouette = locations.current_location(project, binding, "silhouettes", "")
                if silhouette is None:
                    raise ValueError("canonical source publication missing")
                if silhouette["record"].get("source_validation", {}).get("dirty") and not dirty:
                    errors.append("publication cannot clear its source silhouette's dirty assessment")
                if (silhouette["record"].get("field") != field or silhouette["record"].get("label") != label
                        or source.get("image_sha256") != digest(silhouette["image"])
                        or source.get("mask_association") != "silhouette-alpha"):
                    errors.append("source silhouette/classification/image association mismatch")
                evidence = required("evidence", source)
                if evidence is not None:
                    if not isinstance(evidence, list) or (mode == "frame" and not evidence):
                        errors.append("frame-mode generation requires exact source-frame evidence")
                    elif any(not isinstance(item, dict) or item.get("media_id") != binding["media_id"]
                             or type(item.get("frame")) is not int or item["frame"] < 0
                             or not isinstance(item.get("image_sha256"), str) or len(item["image_sha256"]) != 64
                             for item in evidence):
                        errors.append("invalid exact frame evidence association")
                run_path = locations.generation_run_destination(project, record, binding["media_type"], binding["instance_id"],
                                                                 field, label, provider, run.get("number")) / "run.json"
                run_path = locations.project_file(project, run_path)
                payload = json.loads(run_path.read_text())
                if (run.get("sha256") != digest(run_path) or payload.get("source_instance_id") != binding["instance_id"]
                        or payload.get("mode") != mode or payload.get("generation") != generation or payload.get("source") != source):
                    errors.append("published run identity/mode/generation mismatch")
                registered_runs = [row for row in binding.get("runs", []) if row["provider"] == provider and row["number"] == run.get("number")]
                if len(registered_runs) != 1 or registered_runs[0]["state"] != "complete" or registered_runs[0]["mode"] != mode:
                    errors.append("generation run has not been explicitly published")
                elif (registered_runs[0]["run_path"] != str(run_path.relative_to(Path(project).resolve()))
                      or registered_runs[0]["run_sha256"] != run.get("sha256")):
                    errors.append("generation run differs from registry commitment")
                else:
                    for name, sha in json.loads(registered_runs[0]["manifest"]).items():
                        locations.component(name)
                        if digest(locations.project_file(project, run_path.parent / name)) != sha:
                            errors.append("published run artifact checksum mismatch")
            except (ValueError, OSError, TypeError, KeyError):
                errors.append("source publication or generation run unavailable")
    else:
        errors.append("unsupported resource kind")
    state = "invalid" if errors else "historical" if historical else "incomplete" if missing else "valid"
    return {"state": state, "missing": missing, "errors": errors, "dirty": dirty, "destination": destination}