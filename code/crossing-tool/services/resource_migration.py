"""Bounded media-first preparation over the retained research mapping.

No production installation, activation, inference, deletion or rehearsal is exposed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

from data import resource_locations as locations


def transform_silhouette(record, instance_id, *, correction=None):
    from data.media_id import parse_instance_id
    if str(record.get("schema_version", "1")) != "1":
        raise ValueError("Unknown active silhouette schema requires review")
    shot, _ = parse_instance_id(instance_id)
    if shot != record.get("shot_id"):
        raise ValueError("Migration identity differs from the original shot")
    result = copy.deepcopy(record)
    if correction is not None:
        if result.get("field") not in (None, ""):
            raise ValueError("Correction may only fill the missing field")
        result["field"] = correction
    locations.component(result.get("field"))
    if not isinstance(result.get("label"), str) or not result["label"].strip():
        raise ValueError("Original descriptive label is required")
    result.update(publication_contract="media-first-v1", resource_kind="silhouettes", instance_id=instance_id, png="silhouette.png")
    return result


def source_frame_reference_plan(project, record):
    from data.media_id import parse_shot_id
    from services.frame_match import best_frame_path
    original = record.get("source_frame")
    _, start, end = parse_shot_id(record["shot_id"])
    if original == "frame:" + str(record.get("frame")) and type(record.get("frame")) is int and start <= record["frame"] <= end:
        return {"classification": "numeric source-frame marker, not a file", "proposed_target": original, "approval_required": False, "patch": {}}
    if not isinstance(original, str) or not Path(original).is_absolute():
        return {"classification": "missing or contradictory reference", "proposed_target": None, "approval_required": True, "patch": {}}
    project = Path(project).resolve()
    expected = best_frame_path(str(project), record["media_type"], record["filename"], record["shot_id"])
    relative = expected.relative_to(project)
    parts = Path(original).parts
    if tuple(parts[-len(relative.parts):]) != relative.parts:
        return {"classification": "missing or contradictory reference", "proposed_target": None, "approval_required": True, "patch": {}}
    try:
        locations.project_file(project, expected)
    except (OSError, ValueError):
        return {"classification": "missing current BEST candidate", "proposed_target": str(relative), "approval_required": True, "patch": {}}
    return {"classification": "known legacy project-root alias; human approval required", "proposed_target": str(relative), "approval_required": True,
            "observed_legacy_root": str(Path(*parts[:-len(relative.parts)])),
            "proof": "exact media/filename/shot BEST suffix and current regular file existence; old-machine pixel identity is not certified",
            "patch": {"source_frame": str(relative), "historical_source_frame": original}}


def prepare_reference_decisions(project, output):
    """Add pending exact source-frame decisions to an existing preparation package only."""
    project = Path(project).resolve(strict=True)
    output = Path(output)
    output = output if output.is_absolute() else project / output
    if output.relative_to(project).parts[:2] != ("outputs", "maintenance") or ".." in output.parts:
        raise ValueError("Reference decisions belong only in maintenance outputs")
    plan = locations.project_file(project, output / "migration-plan.sqlite3")
    with sqlite3.connect(plan) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA synchronous=FULL")
        database.execute("PRAGMA foreign_keys=ON")
        if json.loads(database.execute("SELECT value FROM meta WHERE key='project'").fetchone()[0]) != str(project):
            raise ValueError("Reference decision project mismatch")
        database.execute("CREATE TABLE reference_decisions(source TEXT PRIMARY KEY,pointer TEXT,original_target TEXT,proposed_target TEXT,classification TEXT,approval_required INTEGER,payload TEXT)")
        for number, row in enumerate(database.execute("SELECT r.*,f.instance_id,f.sha256 FROM reference_plan r JOIN files f ON f.source=r.source WHERE r.pointer='/source_frame' AND (r.target LIKE 'frame:%' OR r.target NOT LIKE ?) ORDER BY r.source", (str(project) + "/%",)), 1):
            original_bytes = locations.project_file(project, row["source"]).read_bytes()
            if sha256(original_bytes) != row["sha256"]:
                raise ValueError("Frozen source changed before reference proposal")
            record = json.loads(original_bytes)
            decision = source_frame_reference_plan(project, record)
            database.execute("INSERT INTO reference_decisions VALUES(?,?,?,?,?,?,?)", (row["source"], row["pointer"], row["target"], decision["proposed_target"], decision["classification"], int(decision["approval_required"]), json.dumps(decision)))
            if decision["classification"] == "numeric source-frame marker, not a file":
                database.execute("UPDATE reference_plan SET classification=?,required=0 WHERE source=? AND pointer='/source_frame'", (decision["classification"], row["source"]))
                database.execute("DELETE FROM issues WHERE category='unresolved-reference' AND detail LIKE ?", (row["target"] + ": %",))
            elif decision["patch"] and row["instance_id"]:
                transformed = {**transform_silhouette(record, row["instance_id"]), **decision["patch"]}
                current = database.execute("SELECT patch FROM transformations WHERE source=?", (row["source"],)).fetchone()
                patch = {**json.loads(current[0]), **decision["patch"]}
                payload = encode(transformed)
                database.execute("UPDATE transformations SET patch=?,after_sha256=?,after_bytes=? WHERE source=?", (json.dumps(patch), sha256(payload), len(payload), row["source"]))
                database.execute("UPDATE reference_plan SET proposed_target=?,classification=? WHERE source=? AND pointer='/source_frame'", (decision["proposed_target"], decision["classification"], row["source"]))
                database.execute("UPDATE files SET reason='proposed exact legacy source-frame root alias; human approval required before installation' WHERE source=?", (row["source"],))
            if number % 4096 == 0:
                database.commit()
        database.commit()
        counts = [tuple(row) for row in database.execute("SELECT classification,count(*) FROM reference_decisions GROUP BY classification")]
        database.execute("INSERT INTO meta VALUES('source_frame_decisions',?)", (json.dumps(counts),))
        database.commit()
    _write_reports(project, output)
    return {"source_frame_decisions": counts, "production_rewrites": 0}


def _reuse_retained_checksums(database, evidence):
    with Path(evidence).open("rb") as stream:
        header = stream.read(100)
    if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise ValueError("Retained checksum evidence requires a committed non-WAL snapshot")
    database.execute("ATTACH DATABASE ? AS retained_checksum_evidence", (Path(evidence).resolve().as_uri() + "?mode=ro",))
    fingerprint = hashlib.sha256()
    matched = 0
    query = """SELECT f.source,p.sha256,f.size,f.mtime_ns FROM files f
               JOIN retained_checksum_evidence.protected p ON p.path=f.source
               WHERE f.sha256 IS NULL AND f.size=p.size AND f.mtime_ns=p.mtime_ns
               AND length(p.sha256)=64 AND p.sha256 NOT GLOB '*[^0-9a-f]*' ORDER BY f.source"""
    for row in database.execute(query):
        database.execute("UPDATE files SET sha256=?,hash_evidence=? WHERE source=? AND sha256 IS NULL", (row[1], "retained archival protected SHA256 with matching current size/mtime; fresh full hash deferred to 2B.3d", row[0]))
        fingerprint.update(json.dumps(tuple(row), separators=(",", ":")).encode() + b"\n")
        matched += 1
        if matched % 4096 == 0:
            database.commit()
    database.commit()
    missing = database.execute("SELECT count(*) FROM files WHERE sha256 IS NULL").fetchone()[0]
    return {"matched_sources": matched, "unfrozen_sources": missing, "selected_evidence_sha256": fingerprint.hexdigest(),
            "source_table": "protected", "fresh_corpus_hashing_performed": False}


def freeze_retained_checksums(project, output):
    """Reuse the existing protected inventory; never refresh the historical census."""
    project = Path(project).resolve(strict=True)
    output = Path(output)
    output = output if output.is_absolute() else project / output
    if output.relative_to(project).parts[:2] != ("outputs", "maintenance") or ".." in output.parts:
        raise ValueError("Checksum preparation belongs only in maintenance outputs")
    plan = locations.project_file(project, output / "migration-plan.sqlite3")
    evidence = locations.project_file(project, "outputs/tests/silhouette-identity-migration/archival-execution/v3-preparation-final/journal.sqlite3")
    with sqlite3.connect(plan, uri=True) as database:
        database.execute("PRAGMA synchronous=FULL")
        database.execute("PRAGMA foreign_keys=ON")
        if json.loads(database.execute("SELECT value FROM meta WHERE key='project'").fetchone()[0]) != str(project):
            raise ValueError("Checksum preparation project mismatch")
        result = _reuse_retained_checksums(database, evidence)
        result["source_database"] = str(evidence.relative_to(project))
        database.execute("INSERT INTO meta VALUES('retained_checksum_evidence',?)", (json.dumps(result),))
        for row in database.execute("SELECT source FROM files WHERE sha256 IS NULL"):
            _issue(database, "missing-frozen-checksum", row[0], "No matching retained protected checksum; explicit fresh preflight hash required before full rehearsal acceptance")
        database.commit()
    _write_reports(project, output)
    return result


def transform_manifest(record, path_mapping, *, legacy_aliases=()):
    if not isinstance(record, dict) or record.get("version") != 1:
        raise ValueError("Unknown active manifest schema requires review")
    if record.get("index_type") not in (None, "annotation-embeddings", "frame-embeddings"):
        raise ValueError("Unknown active manifest index type requires review")
    result = copy.deepcopy(record)
    for section in ("json", "npy", "txt", "valid"):
        descriptor = result.get(section)
        if descriptor is None:
            continue
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("path"), str):
            raise ValueError("Malformed manifest artifact descriptor")
        source = descriptor["path"]
        if source not in path_mapping:
            raise ValueError("Unmapped active manifest reference: " + source)
        destination = path_mapping[source]
        if Path(destination).name != Path(source).name and source not in legacy_aliases:
            raise ValueError("Annotation artifact filenames must be preserved")
        descriptor["path"] = destination
        if "filename" in descriptor:
            descriptor["filename"] = Path(destination).name
    return result


def encode(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def temporary_space(*, metadata_before_bytes, metadata_after_bytes, registry_bytes, replacement_index_bytes, journal_bytes, margin_bytes):
    values = (metadata_before_bytes, metadata_after_bytes, registry_bytes, replacement_index_bytes, journal_bytes, margin_bytes)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("Storage estimates require nonnegative measured byte counts")
    return {"metadata_before_bytes": metadata_before_bytes, "metadata_after_bytes": metadata_after_bytes,
            "registry_staging_and_rollback_bytes": registry_bytes * 2,
            "replacement_index_bytes": replacement_index_bytes, "journal_bytes": journal_bytes,
            "margin_bytes": margin_bytes, "additional_bytes": sum(values) + registry_bytes,
            "binary_corpus_copy_bytes": 0, "strategy": "quiescent same-filesystem rename; transformed metadata before-images and unpublished replacement indexes"}


def readonly(path):
    database = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only=ON")
    return database


def registry_adoption_plan(proposal):
    from data.media_id import parse_instance_id, parse_shot_id
    from data.resource_storage import INSTANCE_REGISTRATION_POLICY
    with readonly(proposal) as database:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or database.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Proposal integrity or foreign-key failure")
        metadata = dict(database.execute("SELECT key,value FROM meta"))
        if metadata.get("schema_version") != "1" or metadata.get("policy") != INSTANCE_REGISTRATION_POLICY:
            raise ValueError("Incompatible identity proposal")
        count = 0
        for row in database.execute("SELECT i.*,s.high_water FROM instances i LEFT JOIN shots s USING(shot_id) ORDER BY i.instance_id"):
            shot, number = parse_instance_id(row["instance_id"])
            if (shot != row["shot_id"] or number != row["instance_number"] or parse_shot_id(shot)[0] != row["media_id"]
                    or row["high_water"] is None or number > row["high_water"]):
                raise ValueError("Inconsistent immutable identity or high-water reservation")
            if len(row["registration_key"]) != 64 or any(character not in "0123456789abcdef" for character in row["registration_key"]):
                raise ValueError("Invalid registration key")
            count += 1
        for key in ("instance_id", "registration_key", "legacy_json"):
            if database.execute(f"SELECT 1 FROM instances GROUP BY {key} HAVING count(*)>1 LIMIT 1").fetchone():
                raise ValueError("Duplicate immutable binding")
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        upgrades = []
        if "resource_locations" not in tables:
            upgrades.append("adopt_registry installs the latest additive ResourceStore schema; do not invoke InstanceRegistry constructor")
        else:
            columns = {row[1]: row for row in database.execute("PRAGMA table_info(publication_operations)")}
            if columns["instance_id"][3]:
                upgrades.append("upgrade_collection_journal after all pending operations are recovered")
            run_sql = database.execute("SELECT sql FROM sqlite_master WHERE name='generation_runs'").fetchone()[0]
            if "reserved" not in run_sql or "reservation" not in {row[1] for row in database.execute("PRAGMA table_info(generation_runs)")}:
                upgrades.append("explicit generation_runs upgrade required; current code has no standalone upgrader; preserve every row and reservation")
        return {"identities": count, "project": metadata.get("project"), "metadata": metadata,
                "shot_namespaces": database.execute("SELECT count(*) FROM shots").fetchone()[0],
                "upgrades": upgrades, "production_adoption_performed": False,
                "owner": "data.resource_storage.adopt_registry", "initial_location_revision": 1,
                "initial_annotation_revision": 1, "allocation": "existing InstanceRegistry.register inside ResourceStore transaction, after six approvals only"}


def correction_proposal(project, reference):
    from data.media_id import label_slug
    from data.resource_storage import digest, relative_path
    from services.resource_corrections import propose_missing_field
    from services.resource_validation import source_annotation
    path = locations.project_file(project, reference)
    record = json.loads(path.read_text())
    if record.get("field") not in (None, ""):
        raise ValueError("Not an unresolved missing-field case")
    annotation_path, annotation = source_annotation(project, record["filename"], record["media_type"], record["shot_id"])
    candidates = []
    for field, raw in annotation.items():
        if field in ("description", "text"):
            continue
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            if isinstance(value, str) and value.strip() and label_slug(value) == label_slug(record["label"]):
                candidates.append({"field": field, "value": value})
    proposal = propose_missing_field(project, reference, candidates[0]["field"], candidates[0]["value"]) if len(candidates) == 1 else None
    image = locations.project_file(project, path.with_suffix(".png"))
    return {"reference": str(reference), "media_id": record["media_id"], "filename": record["filename"],
            "shot_id": record["shot_id"], "frame": record["frame"], "label": record["label"],
            "annotation_reference": relative_path(project, annotation_path), "annotation_sha256": digest(annotation_path),
            "before_sha256": digest(path), "image_sha256": digest(image), "source_frame": record.get("source_frame"),
            "annotation": annotation, "evidence_candidates": candidates, "proposed_field": candidates[0]["field"] if proposal else None,
            "minimal_patch": {"field": candidates[0]["field"]} if proposal else None,
            "owner_proposal": proposal, "human_approval_required": True, "instance_id": None,
            "decision": "approve exact field-only correction" if proposal else "human classification decision; no current exact atomic vocabulary match",
            "available_classifications": {field: annotation.get(field) for field in ("setting", "objects", "humans", "animals", "action", "wearing")},
            "mapping_effect": "retain Review pair; after approval correct via existing owner, then allocate above the retained shot high-water; no existing ID changes"}


def transform_engraving(record, source_record, instance_id, source_pair, publication_pair, run_reference, *, generation=None, canonical_source=None, published_run=None):
    if str(record.get("schema_version")) != "2" or record.get("status") != "generated" or record.get("mode") not in locations.MODES:
        raise ValueError("Unknown active engraving schema requires review")
    result = copy.deepcopy(record)
    result.update(publication_contract="media-first-v1", resource_kind="engravings", source_instance_id=instance_id)
    for key in ("media_id", "media_type", "filename", "filename_stem", "shot_id", "frame", "field", "label"):
        result[key] = source_record[key]
    result["historical_source"] = copy.deepcopy(result["source"])
    result["source"] = copy.deepcopy(canonical_source if canonical_source is not None else result["source"])
    for key in ("silhouette_json", "silhouette_png", "source_json"):
        result["source"].pop(key, None)
    if generation is not None:
        result["generation"] = copy.deepcopy(generation)
    if published_run is not None:
        result["published_run"] = copy.deepcopy(published_run)
    result["output_png"] = str(publication_pair["image"])
    result.setdefault("provenance", {})["run_json"] = run_reference
    return result


def _prepare_engravings(database, project, research, baseline, media_record):
    from data.resource_storage import digest
    from services.resource_validation import validate_generation
    rows = list(research.execute("SELECT * FROM engravings ORDER BY source"))
    for publication in (row for row in rows if row["kind"] == "published" and row["source"].endswith(".json")):
        source = publication["source"]
        record = json.loads((project / source).read_text())
        silhouette = json.loads((project / publication["reference"]).read_text())
        instance = publication["instance_id"]
        media = media_record(silhouette["media_type"], silhouette["media_id"])
        pair = locations.engraving_destination(project, media, silhouette["media_type"], instance, silhouette["field"], silhouette["label"], record["mode"])
        source_pair = locations.silhouette_destination(project, media, silhouette["media_type"], instance, silhouette["field"], silhouette["label"])
        run_directory = locations.generation_run_destination(project, media, silhouette["media_type"], instance, silhouette["field"], silhouette["label"], "local", 1)
        run_source = next(row["source"] for row in rows if row["kind"] != "published" and row["source"].endswith("/run.json"))
        run = json.loads((project / run_source).read_text())
        run_parent = Path(run_source).parent
        artifacts = {}
        artifact_hashes = {}
        for row in rows:
            if row["kind"] == "published":
                target = pair["metadata"] if row["source"].endswith(".json") else pair["image"]
                disposition = "Transform" if row["source"].endswith(".json") else "Move"
            else:
                relative = Path(row["source"]).relative_to(run_parent)
                name = "provider-run.json" if relative == Path("run.json") else "-".join(relative.parts)
                target = run_directory / name
                disposition = "Move"
                artifacts[name] = row["source"]
            path = _file(database, project, row["source"], target, disposition, "services.resource_publication", instance_id=instance, mode=record["mode"], hash_bytes=True, baseline=baseline,
                         reason="immutable provider evidence; byte-for-byte relocation, old paths remain historical" if row["kind"] != "published" else "mode-specific selected publication")
            if path and row["kind"] != "published":
                artifact_hashes[name] = digest(path)
        generation = copy.deepcopy(record["generation"])
        prompts = [(stage["stage"], stage["prompt"]["prompt"]) for stage in run.get("stages", []) if isinstance(stage.get("prompt"), dict) and stage["prompt"].get("prompt")]
        generation.update(prompt="\n\n".join(stage + ":\n" + prompt for stage, prompt in prompts), parameters=run["config"],
                          software={"python": run["environment"]["python"], "torch": run["environment"]["device"]["torch_version"], "cuda": run["environment"]["device"]["cuda_version"]},
                          model_version=run["model"]["pipeline_repo"])
        findings = validate_generation(generation, "local")
        if findings["missing"] or findings["errors"]:
            _issue(database, "generation-reproduction", source, json.dumps(findings))
        annotation_path = locations.annotation_path(project, silhouette["filename"], silhouette["media_type"])
        shot = next(entry["shot"] for entry in json.loads(annotation_path.read_text()) if entry["shot"]["shot_id"] == silhouette["shot_id"])
        best = shot.get("best_frame", {}).get("frame")
        if type(best) is not int:
            _issue(database, "generation-evidence", source, "BEST PNG lacks an exact committed numeric selection; do not infer from extraction frame")
        frame_path = locations.project_file(project, record["source"]["source_frame"])
        canonical_source = {key: copy.deepcopy(value) for key, value in record["source"].items() if key not in ("silhouette_json", "silhouette_png", "source_json")}
        canonical_source.update(image_sha256=digest(project / publication["reference"].replace(".json", ".png")), mask_association="silhouette-alpha",
                                evidence=[{"media_id": silhouette["media_id"], "frame": best, "image_sha256": digest(frame_path), "selection_role": "committed BEST PNG", "query_specific_frame": silhouette["frame"]}])
        payload = {"source_instance_id": instance, "mode": record["mode"], "generation": generation, "source": canonical_source,
                   "historical_import": {"provider_run_sha256": digest(project / run_source), "provider_run_artifact": "provider-run.json", "numeric_frame_pixel_equivalence_certified": False}}
        wrapper = json.dumps({**payload, "artifacts": artifact_hashes}, sort_keys=True, allow_nan=False).encode()
        run_reference = str((run_directory / "run.json").relative_to(project))
        run_plan = {"instance_id": instance, "provider": "local", "proposed_number": 1, "reservation_performed": False,
                    "require_next_reservation_number": 1, "source": run_source, "destination": run_reference, "payload": payload,
                    "artifacts": artifacts, "artifact_hashes": artifact_hashes, "wrapper_sha256": sha256(wrapper), "wrapper_bytes": len(wrapper),
                    "owner": "ResourceStore.reserve_generation_run then publish_generation_run", "mode": record["mode"]}
        database.execute("INSERT INTO meta VALUES(?,?)", ("generation_run_plan", json.dumps(run_plan, ensure_ascii=False)))
        transformed = transform_engraving(record, silhouette, instance, source_pair, pair, run_reference, generation=generation,
                                         canonical_source=canonical_source, published_run={"provider": "local", "number": 1, "sha256": sha256(wrapper)})
        _transform(database, source, "engraving-v2", transformed)
        for pointer, target in record["source"].items():
            if pointer in ("silhouette_json", "silhouette_png", "source_frame"):
                proposed = str(source_pair["metadata" if pointer.endswith("json") else "image"].relative_to(project)) if pointer.startswith("silhouette_") else target
                _reference(database, source, "/source/" + pointer, target, proposed, "identity-based source relationship" if pointer.startswith("silhouette_") else "retained BEST evidence")
        _reference(database, source, "/output_png", record["output_png"], str(pair["image"].relative_to(project)), "path relocation")
        _reference(database, source, "/provenance/run_json", record["provenance"]["run_json"], run_reference, "canonical wrapper over immutable provider evidence")
    database.commit()


def _prepare_remaining(database, project, research, baseline):
    for row in research.execute("SELECT * FROM unmoved_silhouette_artifacts ORDER BY path"):
        _file(database, project, row["path"], None, "Retain", "services.silhouette_curation / existing extraction infrastructure", reason=row["disposition"], baseline=baseline)
    families = {"data/indexes": "Rebuild", "data/vocabulary": "Rebuild", "data/metadata": "Retain", "data/shotlists": "Retain", "data/palettes": "Retain"}
    owners = {"data/indexes": "services.illustration_index / corpus_stats / silhouette viability", "data/vocabulary": "services.vocabulary_index / derived_vocabulary"}
    for scope, disposition in families.items():
        root = project / scope
        if not root.exists():
            continue
        for parent, directories, filenames in os.walk(root, followlinks=False):
            directories.sort()
            for filename in sorted(filenames):
                source = str((Path(parent) / filename).relative_to(project))
                _file(database, project, source, None, disposition, owners.get(scope, "canonical data owner; unchanged"), hash_bytes=True, baseline=baseline,
                      reason="preserve current file until an unpublished replacement passes; no rebuild during 2B.3c" if disposition == "Rebuild" else "unchanged input, outside resource relocation")
                if disposition == "Rebuild":
                    database.execute("INSERT INTO index_transitions VALUES(?,?,?,?)", (source, owners[scope], "build replacement through existing owner; preserve old bytes and revision", "all canonical files and commitments verified; cohort barrier"))
    for row in research.execute("SELECT * FROM references_found WHERE classification NOT LIKE 'authoritative%'"):
        _reference(database, row["source"], row["pointer"], row["target"], row["target"], "immutable historical or Book-local reference; retain unchanged", False)
    for scope, disposition in (("archive/silhouettes", "Exclude"), ("outputs/books", "Retain"), ("outputs/tests", "Retain"), ("media/frames/best", "Retain"), ("preferences", "Retain")):
        count = baseline.execute("SELECT count(*) FROM files WHERE kind='file' AND path LIKE ?", (scope + "/%",)).fetchone()[0]
        evidence = {"database": "outputs/tests/media-first-migration/safety-final.sqlite3", "query": "SELECT * FROM files WHERE kind='file' AND path LIKE '" + scope + "/%'", "retained_count": count,
                    "rule": "immutable outside migration; no new historical census; BEST dependencies inspected by exact active reference only"}
        database.execute("INSERT INTO inventory_scopes VALUES(?,?,?,0)", (scope, disposition, json.dumps(evidence)))
    database.commit()


def _reconcile_indexes(database, project):
    database.execute("CREATE TABLE active_index_membership(reference TEXT PRIMARY KEY,media_type TEXT,field TEXT,label TEXT,shot_id TEXT)")
    for media_type in ("movie", "gameplay"):
        path = project / "data/indexes/illustration" / (media_type + "-silhouettes.sqlite3")
        with readonly(path) as index:
            for row in index.execute("SELECT payload FROM records WHERE assignment_state='active'"):
                record = json.loads(row[0])
                source = Path(record["path"])
                source = str(source.relative_to(project)) if source.is_absolute() else str(source)
                try:
                    database.execute("INSERT INTO active_index_membership VALUES(?,?,?,?,?)", (source, media_type, record.get("field"), record.get("label"), record.get("shot_id")))
                except sqlite3.IntegrityError:
                    _issue(database, "index-duplicate", source, "Duplicate active index source")
        database.commit()
    for row in database.execute("SELECT a.reference FROM active_index_membership a LEFT JOIN silhouettes s ON a.reference=s.legacy_json WHERE s.legacy_json IS NULL"):
        _issue(database, "index-added", row[0], "Current active index source absent from frozen research")
    for row in database.execute("SELECT s.legacy_json FROM silhouettes s LEFT JOIN active_index_membership a ON a.reference=s.legacy_json WHERE a.reference IS NULL"):
        _issue(database, "index-missing", row[0], "Research active source absent from current index")
    for row in database.execute("SELECT s.legacy_json FROM silhouettes s JOIN active_index_membership a ON a.reference=s.legacy_json WHERE a.media_type IS NOT s.media_type OR a.field IS NOT s.field OR a.label IS NOT s.label OR a.shot_id IS NOT s.shot_id"):
        _issue(database, "index-binding", row[0], "Current indexed classification or immutable association differs")
    database.commit()


def _check_references(database, project):
    for row in database.execute("SELECT target,required,min(source) source FROM reference_plan WHERE pointer!='/png' AND classification NOT IN ('legacy alias','existing stale cache') GROUP BY target,required"):
        if not row["required"]:
            continue
        target = row["target"]
        try:
            if database.execute("SELECT 1 FROM files WHERE source=?", (target,)).fetchone():
                continue
            locations.project_file(project, target)
        except (ValueError, OSError) as exc:
            _issue(database, "unresolved-reference", row["source"], target + ": " + str(exc))
    database.commit()


def _check_membership(database, project):
    for scope in ("data/annotations", "data/silhouettes", "data/engravings"):
        count = 0
        for parent, directories, filenames in os.walk(project / scope, followlinks=False):
            for directory in directories:
                if (Path(parent) / directory).is_symlink():
                    _issue(database, "unexpected-symlink", str((Path(parent) / directory).relative_to(project)), "Source ancestry cannot be migrated")
            for filename in filenames:
                source = str((Path(parent) / filename).relative_to(project))
                count += 1
                if database.execute("SELECT 1 FROM files WHERE source=?", (source,)).fetchone() is None:
                    _file(database, project, source, None, "Review", "unmapped current source", reason="not in retained inventory; explicit owner decision required")
                    _issue(database, "unexpected-source", source, "Relevant current source absent from retained mapping")
        database.execute("INSERT INTO inventory_scopes VALUES(?,?,?,1)", (scope, "mapped individually", json.dumps({"current_file_count": count, "method": "bounded current namespace membership; no historical lifecycle census or allocation experiment"})))
        database.commit()


def rehearsal(project, plan, *, approved=False):
    """Stage 2B.3d read-only full mapping traversal; never called by preparation."""
    from data.resource_storage import digest
    if not approved:
        raise ValueError("Separate Stage 2B.3d rehearsal approval required")
    checked = 0
    failures = []
    failure_count = 0
    coverage = {"identities": 0, "collections": 0, "references": 0, "index_rows": 0, "generation_runs": 0, "retained_dependencies": 0}
    dependency_fingerprint = hashlib.sha256()
    def fail(source, message):
        nonlocal failure_count
        failure_count += 1
        if len(failures) < 128:
            failures.append({"source": str(source), "error": str(message)})
    with readonly(plan) as database:
        metadata = {key: json.loads(value) for key, value in database.execute("SELECT key,value FROM meta")}
        if metadata["project"] != str(Path(project).resolve()):
            raise ValueError("Rehearsal project mismatch")
        for source, expected in metadata["inputs"].items():
            if digest(locations.project_file(project, source)) != expected:
                raise ValueError("Frozen preparation input changed")
        for row in database.execute("SELECT * FROM files ORDER BY source"):
            try:
                path = locations.project_file(project, row["source"])
                info = path.stat()
                if (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev, info.st_nlink) != tuple(row[key] for key in ("size", "mtime_ns", "ctime_ns", "inode", "device", "links")):
                    raise ValueError("Source fingerprint changed")
                if row["sha256"] is None or digest(path) != row["sha256"]:
                    raise ValueError("Missing or changed frozen content hash")
                if row["destination"] and (Path(project) / row["destination"]).exists():
                    raise ValueError("Destination collision")
                transform = database.execute("SELECT * FROM transformations WHERE source=?", (row["source"],)).fetchone()
                if transform:
                    record = json.loads(path.read_text())
                    patch = json.loads(transform["patch"])
                    result = {**record, **patch} if transform["schema"] == "silhouette-v1" else patch
                    if sha256(encode(result)) != transform["after_sha256"]:
                        raise ValueError("Proposed transformed bytes differ")
                checked += 1
            except (OSError, ValueError, TypeError) as exc:
                fail(row["source"], exc)
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or database.execute("PRAGMA foreign_key_check").fetchone():
            fail(plan, "Plan integrity/foreign-key failure")
        proposal_relative = next(source for source in metadata["inputs"] if source.endswith("registry-proposal.sqlite3"))
        identity_check = registry_adoption_plan(locations.project_file(project, proposal_relative))
        coverage["identities"] = identity_check["identities"]
        if coverage["identities"] != database.execute("SELECT count(*) FROM identity_bindings").fetchone()[0]:
            fail(plan, "Identity preservation count differs")
        database.execute("ATTACH DATABASE ? AS frozen_proposal", (locations.project_file(project, proposal_relative).as_uri() + "?mode=ro",))
        for query in ("SELECT * FROM identity_bindings EXCEPT SELECT * FROM frozen_proposal.instances",
                      "SELECT * FROM frozen_proposal.instances EXCEPT SELECT * FROM identity_bindings",
                      "SELECT * FROM shot_reservations EXCEPT SELECT * FROM frozen_proposal.shots",
                      "SELECT * FROM frozen_proposal.shots EXCEPT SELECT * FROM shot_reservations"):
            if database.execute(query + " LIMIT 1").fetchone():
                fail(plan, "Frozen bindings/high-water reservations changed")
        from services.resource_validation import validate_annotation_classifications, validate_generation
        from data.media_id import parse_shot_id
        for collection in database.execute("SELECT * FROM collections ORDER BY media_type,media_id"):
            artifacts = json.loads(collection["artifacts"])
            try:
                for suffix, artifact in artifacts.items():
                    source = locations.project_file(project, artifact["source"])
                    if digest(source) != artifact["sha256"]:
                        raise ValueError("Collection commitment source hash changed")
                    if suffix.endswith(".npy"):
                        import numpy as np
                        array = np.load(source, mmap_mode="r", allow_pickle=False)
                        if suffix == ".frames.valid.npy":
                            if array.ndim != 1 or str(array.dtype) != "bool":
                                raise ValueError("Frame validity mask schema differs")
                        elif array.ndim != 2 or str(array.dtype) != "float32":
                            raise ValueError("Embedding schema differs")
                entries = json.loads(locations.project_file(project, artifacts[".annotations.json"]["source"]).read_text())
                seen = set()
                for entry in entries:
                    shot = entry["shot"]
                    media_id, start, end = parse_shot_id(shot["shot_id"])
                    if media_id != collection["media_id"] or shot["shot_id"] in seen:
                        raise ValueError("Collection source shot ownership/uniqueness differs")
                    seen.add(shot["shot_id"])
                    if validate_annotation_classifications(shot.get("annotation", {})):
                        raise ValueError("Collection classifications contradict source vocabulary")
                    if isinstance(shot.get("best_frame"), dict):
                        frame = shot["best_frame"].get("frame")
                        if type(frame) is not int or not start <= frame <= end:
                            raise ValueError("Committed BEST numeric selection is outside shot")
                coverage["collections"] += 1
            except (OSError, ValueError, KeyError, TypeError) as exc:
                fail(collection["filename"], exc)
        for reference in database.execute("SELECT * FROM reference_plan ORDER BY source,pointer"):
            coverage["references"] += 1
            if not reference["required"]:
                continue
            proposed = reference["proposed_target"]
            if proposed is None:
                fail(reference["source"], "Unresolved proposed reference")
                continue
            candidate = Path(proposed)
            try:
                relative = str(candidate.relative_to(Path(project).resolve())) if candidate.is_absolute() else str(candidate)
                mapped = database.execute("SELECT source FROM files WHERE destination=?", (relative,)).fetchone()
                if mapped:
                    locations.project_file(project, mapped[0])
                elif metadata.get("generation_run_plan", {}).get("destination") != relative:
                    locations.project_file(project, proposed)
            except (OSError, ValueError) as exc:
                fail(reference["source"], exc)
        for reference in database.execute("SELECT proposed_target FROM reference_plan WHERE required=1 AND pointer='/source_frame' GROUP BY proposed_target ORDER BY proposed_target"):
            if reference[0] is None:
                continue
            try:
                path = locations.project_file(project, reference[0])
                dependency_fingerprint.update((reference[0] + "\0" + digest(path) + "\n").encode())
                coverage["retained_dependencies"] += 1
            except (OSError, ValueError) as exc:
                fail(reference[0], exc)
        for media_type in ("movie", "gameplay"):
            index_path = Path(project) / "data/indexes/illustration" / (media_type + "-silhouettes.sqlite3")
            with readonly(index_path) as index:
                if index.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    fail(index_path, "Index integrity failure")
                for row in index.execute("SELECT payload FROM records WHERE assignment_state='active'"):
                    record = json.loads(row[0])
                    source = Path(record["path"])
                    relative = str(source.relative_to(Path(project).resolve())) if source.is_absolute() else str(source)
                    expected = database.execute("SELECT * FROM silhouettes WHERE legacy_json=?", (relative,)).fetchone()
                    if expected is None or any(expected[key] != record.get(key) for key in ("media_id", "shot_id", "field", "label")):
                        fail(relative, "Active index source/classification differs")
                    coverage["index_rows"] += 1
        if coverage["index_rows"] != database.execute("SELECT count(*) FROM silhouettes").fetchone()[0]:
            fail(plan, "Complete active index membership count differs")
        run = metadata.get("generation_run_plan")
        if run:
            findings = validate_generation(run["payload"]["generation"], run["provider"])
            if findings["missing"] or findings["errors"]:
                fail(run["source"], json.dumps(findings))
            for name, source in run["artifacts"].items():
                if digest(locations.project_file(project, source)) != run["artifact_hashes"][name]:
                    fail(source, "Immutable run artifact changed")
            wrapper = json.dumps({**run["payload"], "artifacts": run["artifact_hashes"]}, sort_keys=True, allow_nan=False).encode()
            if sha256(wrapper) != run["wrapper_sha256"]:
                fail(run["source"], "Canonical run wrapper/source relationship changed")
            coverage["generation_runs"] = 1
        blocking = database.execute("SELECT count(*) FROM issues WHERE blocking=1").fetchone()[0]
        withheld = database.execute("SELECT count(*) FROM corrections").fetchone()[0]
        return {"files_checked": checked, "coverage": coverage, "retained_dependency_fingerprint": dependency_fingerprint.hexdigest(), "failures": failures, "failure_count": failure_count, "preparation_blockers": blocking, "withheld": withheld,
            "mapping_checks_passed": not failure_count and not blocking and not withheld, "accepted": False,
            "remaining_gates": ["source/classification and collection validation through existing owners", "fixture registry/adoption/publication/index checks", "reference traversal and full source/index membership reconciliation", "verified recovery source", "human release approval"],
            "physical_installation_performed": False}


def _write_reports(project, output):
    import inspect
    import shutil
    from data.annotate import atomic_write_text
    with readonly(output / "migration-plan.sqlite3") as database:
        metadata = {key: json.loads(value) for key, value in database.execute("SELECT key,value FROM meta")}
        counts = {row[0]: row[1] for row in database.execute("SELECT disposition,count(*) FROM files GROUP BY disposition")}
        active = database.execute("SELECT count(*) FROM silhouettes").fetchone()[0]
        identities = database.execute("SELECT count(*) FROM identity_bindings").fetchone()[0]
        corrections = [json.loads(row[0]) for row in database.execute("SELECT payload FROM corrections ORDER BY reference")]
        issues = [tuple(row) for row in database.execute("SELECT category,count(*),blocking FROM issues GROUP BY category,blocking")]
        references = [tuple(row) for row in database.execute("SELECT classification,count(*) FROM reference_plan GROUP BY classification")]
        groups = [tuple(row) for row in database.execute("SELECT media_type,count(*) FROM active_index_membership GROUP BY media_type")]
        before, after = database.execute("SELECT coalesce(sum(f.size),0),coalesce(sum(t.after_bytes),0) FROM transformations t JOIN files f ON f.source=t.source").fetchone()
        index_bytes = database.execute("SELECT coalesce(sum(size),0) FROM files WHERE disposition='Rebuild'").fetchone()[0]
        proposal_bytes = (project / "outputs/tests/silhouette-canonical-identity/registry-proposal.sqlite3").stat().st_size
        file_count = database.execute("SELECT count(*) FROM files").fetchone()[0]
        binary_bytes = database.execute("SELECT coalesce(sum(size),0) FROM files WHERE disposition='Move'").fetchone()[0]
        collections = database.execute("SELECT count(*) FROM collections").fetchone()[0]
        annotation_files = database.execute("SELECT count(*) FROM files WHERE source LIKE 'data/annotations/%'").fetchone()[0]
        engraving_files = database.execute("SELECT count(*) FROM files WHERE source LIKE 'data/engravings/%'").fetchone()[0]
        unsupported = database.execute("SELECT count(*) FROM issues WHERE blocking=1").fetchone()[0]
        integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = list(database.execute("PRAGMA foreign_key_check"))
    measured_plan_bytes = (output / "migration-plan.sqlite3").stat().st_size
    space = temporary_space(metadata_before_bytes=before, metadata_after_bytes=after, registry_bytes=proposal_bytes + measured_plan_bytes,
                        replacement_index_bytes=index_bytes * 2, journal_bytes=max(measured_plan_bytes, file_count * 1024), margin_bytes=4 * 1024**3)
    free = shutil.disk_usage(project).free
    space.update(available_bytes=free, current_plan_bytes=measured_plan_bytes, immutable_binary_bytes_renamed=binary_bytes,
                 minimum_additional_inodes=2 * file_count + identities + 2 * collections + 1000,
                 available_inodes=os.statvfs(project).f_favail, capacity_check=free >= space["additional_bytes"],
                 estimates_not_exact_allocation=True, index_growth_allowance="2x current derived-index bytes; registry allowance includes full plan size",
                 future_full_hash_seconds_estimate=binary_bytes / (100 * 1024**2),
                 future_move_seconds_historical_rate_estimate=file_count / (1661512 / 1570.112))
    atomic_write_text(output / "summary.json", json.dumps({"active_records": active, "identities": identities, "withheld": len(corrections),
                      "annotation_files": annotation_files, "annotation_collections": collections, "engraving_files": engraving_files,
                      "file_count": file_count, "dispositions": counts, "issues": issues, "references": references,
                      "active_indexes": groups, "source_frame_decisions": metadata.get("source_frame_decisions", []), "retained_checksum_evidence": metadata.get("retained_checksum_evidence", {}), "space": space, "integrity": integrity, "foreign_key_errors": len(foreign_keys),
                      "stage_2b3d_execution_authorized": False, "physical_migration_ready": False}, indent=2) + "\n")
    correction_text = ["# Six Correction Proposals\n", "Production records and PNGs remain unchanged. No final identity or run ordinal was allocated.\n"]
    for number, case in enumerate(corrections, 1):
        correction_text.extend([f"## {number}. {case['filename']} / {Path(case['reference']).name}\n",
                f"Media `{case['media_id']}`; shot `{case['shot_id']}`; query frame {case['frame']}; label `{case['label']}`.\n",
                f"JSON: `{case['reference']}`. Annotation: `{case['annotation_reference']}`.\n",
                f"Source-frame reference: `{case['source_frame']}`. Its existing PNG is preserved, not certified as the query-specific frame.\n",
                f"Current atomic classification evidence: `{json.dumps(case['available_classifications'], ensure_ascii=False)}`.\n",
                f"Description (context, not atomic vocabulary): {case['annotation'].get('description', '')}\n",
                f"Proposed field: `{case['proposed_field']}`. Minimal patch: `{json.dumps(case['minimal_patch'])}`. Human approval REQUIRED.\n",
                f"{case['mapping_effect']}. Existing immutable IDs remain fixed; destination stays NULL until approval and allocation.\n",
                "" if case["proposed_field"] else "No current category contains the exact field/fields label. Setting camp/coast is not evidence for setting field/fields. Objects is legitimate if an object classification is curatorially established and its exact source annotation value approved; it is not currently evidenced by the empty/nonmatching objects list. Do not silently edit annotations or labels.\n",
                f"JSON SHA256 `{case['before_sha256']}`; annotation SHA256 `{case['annotation_sha256']}`; PNG SHA256 `{case['image_sha256']}`.\n"])
    reports = {
            "correction-proposals.md": "\n".join(correction_text),
            "registry-adoption.md": f"""# Registry Adoption Preparation

        This is an adoption plan, not a production registry. `migration-plan.sqlite3.identity_bindings` and `shot_reservations` are immutable proposal evidence, not an allocator or authority. Existing bindings: {identities:,}. Six exact exceptions: {len(corrections)}. Proposal integrity and foreign keys passed; source hashes are frozen in `meta.inputs`.

        1. Stop all CLI/Qt/MCP processes, jobs and rebuilds under an operator-enforced maintenance barrier. Obtain a trustworthy recovery source separately. Confirm the proposal project, policy, integrity, all IDs/keys/media/shot associations and high-water limits; use `registry_adoption_plan` and the full rehearsal, never rerun allocation.
        2. Obtain six individual classification approvals. `services.resource_corrections.propose_missing_field/apply_missing_field` bind exact JSON and annotation hashes and field-only before-images. Two proposals are evidence-supported; four require new curator evidence/decisions. No existing ID may change. Resolve all six before complete activation. Exceptions permit unpublished adoption preparation, not a mixed-layout release.
        3. Under separate execution approval use `data.resource_storage.adopt_registry`, copying the retained proposal into an explicitly unpublished registry below `data/indexes/`. Do not invoke the research InstanceRegistry constructor on it. Preserve the proposal unchanged and retain its hash. Adopt with exact exception references/reasons; after approved correction register only those six through the existing non-recycling allocator inside ResourceStore, retaining every preexisting high-water reservation.
        4. New adoption installs latest resource_locations, annotation_layouts, generation_runs and nullable-instance publication_operations schemas. No upgrade is needed for this plain proposal beyond adoption. Existing adopted deployments, if encountered, require `upgrade_collection_journal` only after recovery. Older generation_runs schemas fail closed; a standalone upgrader is not implemented and would be a genuine prerequisite, not an implicit read-time upgrade. Preserve reserved/failed/abandoned/prepared/complete rows, tokens and MAX+1 numbering across modes.
        5. Do not commit a canonical row before its installed JSON/PNG hashes and exact destinations are verified. Initial location revisions are 1; subsequent changes require loaded CAS. Use the existing ResourceStore/pair-publication owner, not SQL that re-blesses stale commitments. Initial annotation layout adoption uses `commit_annotation_layout` only once after exact membership installation; later collection transitions use `publish_annotation_collection` and recovery.
        6. Collections: {collections}; artifacts: {annotation_files}. Keep all optional physical members including any committed logs; preserve stale cache hashes as stale, never call a path correction recomputation. For controlled frame-manifest updates the owner requires array/mask/manifest pairing. Keep filename/media bindings exact, original BEST metadata and images intact.
        7. Import the one historical local run through `reserve_generation_run` then `publish_generation_run`: proposed next number 1 must equal the actual reservation; otherwise stop/refreeze. Preserve provider-run.json and all seven evidence files exactly, use the retained prompts/settings/seed/environment/model release in the wrapper. `meta.generation_run_plan` contains the exact payload, artifact paths/hashes and expected wrapper hash. Then publish the selected frame pair with `published_run` through the existing owner; no isolated publication is invented.
        8. Fully verify the unpublished registry and all location/layout/run commitments before coordinating its final path and replacement indexes under the maintenance barrier. Index payloads must reference the final registry path, not an adopting/staging path. Normal launchers receive the explicit final registry only after the entire cohort passes. Directory existence alone never activates it.
        9. Recovery uses existing journal before-images and captured revisions. Never recycle IDs/run numbers, lower high-water marks, overwrite an adopted registry, or roll current locations back independently from their files/index cohort. Retain interrupted snapshots and new reservations as evidence; restore legacy operation without selecting the failed canonical registry.

        Schema decisions: `{json.dumps(metadata['adoption']['upgrades'])}`. No production registry was created, upgraded or activated in Stage 2B.3c.
        """,
            "reference-transformations.md": f"""# Stored Reference Transformations

        Exact per-file transforms are queryable in `transformations`; exact references/classifications in `reference_plan`. Unknown schemas and contradictory references are Review/issues, never blind string replacement. Current reference classification counts: `{json.dumps(references)}`.

        | Schema / writer / reader | Transformation | Preservation and check |
        | --- | --- | --- |
        | Annotation shot-list JSON; data.annotate / registered annotation loaders | Same bytes and basename, media-item directory relocation only | All shot IDs, text, annotation values, BEST frame/score/method/provenance and unknown keys unchanged |
        | Annotation manifest v1; data.index / embedding loaders | Only json/txt/npy descriptor path and descriptor filename | Exact old .json/.txt/.npy aliases map to the existing .annotations.* artifact; preserve source/member hashes and array bytes. Missing/contradictory pointers block; old stale hashes remain stale |
        | Frame manifest v1; frame_embeddings / Sync | Only json/npy/valid path and filename | No CLIP/SAM/decoding, frame/valid arrays byte-identical, dimensions and counts preserved; stale source hashes are not repaired |
        | Silhouette catalog schema 1; silhouette_catalog / resource_resolution | Add immutable instance/resource contract, png becomes silhouette.png | Preserve original labels, fields, frame, mask/bbox/source-frame/assignment/review/human_best/dirty/search_provenance and all unknown metadata; exact SHA of proposed serialization retained |
        | Generated engraving schema 2; local publisher / resource_resolution | source_instance_id and canonical mode pair; live source uses image hash/mask association/evidence, obsolete source paths removed from live namespace | Preserve original source dictionary as historical_source. Keep all generation facts, enrich only from the actual retained run, preserve review/dirty and original nested silhouette metadata. No backend-as-mode or legacy ordinal identity |
        | Historical local provider run schema 1; local generation / provenance inspection | Byte-identical relocation to provider-run.json, plus a new canonical wrapper through existing publisher | Old paths inside immutable run remain forensic references; file mapping supplies explicit old-to-new evidence locations. Do not reinterpret them as registry dependencies. Flat artifact names required by the existing publisher; evidence/mask.png becomes evidence-mask.png without changing bytes |
        | Illustration SQLite payloads; illustration_index / discovery / resolve_index_record | Rebuild through current owner, not direct SQL path rewriting | Preserve lifecycle, field/label/shot_type, mode, ID/navigation, movie/gameplay, registry path and location revision. Compare full membership to active_index_membership |
        | Corpus/vocabulary/viability/cache and Sync relationships; existing owners | Context/revision-aware invalidation and unpublished replacement where persisted | Preserve old production files until release; arrays and pairing are not regenerated merely for directory relocation |
        | Archive, research, journals, Book-relative images | Retain unchanged; explicit historical lookup only | Not ordinary active discovery or rollback scratch. Old output-local preprocessing paths do not become live resource dependencies |

        BEST PNGs stay at existing media/frames/best paths. Query-specific extraction frame and committed BEST numeric selection are separate retained facts. Pixel equivalence is not retroactively certified. Frame-mode evidence includes the actual committed BEST number and its existing PNG hash, plus the original query frame; no frame is regenerated. Valid frame:N markers are numeric provenance, not missing files. Old absolute project-root paths receive only exact media/filename/shot suffix proposals through services.frame_match.best_frame_path, requiring approval; proposed source_frame points to the existing current PNG while historical_source_frame retains the original path. Pending decisions and their exact current-file evidence are in reference_decisions; no old-machine pixel equivalence is claimed.

        Initial collection commitment captures exact installed bytes including stale derived manifests. Staleness is still reported by cache readers; it does not fabricate a fresh embedding source hash. Subsequent regeneration must meet the existing controlled publication contract. Existing unknown-generation facts are never invented; reproducibility validation uses the actual run's two prompts, complete config, seed 42, Python/Torch/CUDA and Qwen release.

        Fixtures: six exact source-evidence cases, alias-only rewrites and stale hashes, original labels/BEST/unknown fields, generation/review/seed preservation, schema failure and same-resource path mapping. The Stage 2B.3d full gate additionally resolves every proposed active reference through installed commitments before activation.
        """,
            "execution-and-recovery.md": f"""# Execution And Recovery

        Stage 2B.3c performs preparation only. No production movement/activation/deletion command is provided. Production physical migration requires a separate explicit authorization AFTER Stage 2B.3d rehearsal and human decisions.

        1. **Source preflight:** freeze this plan, proposal and source metadata hashes; run the entire rehearsal; freshly hash EVERY JSON/PNG/array/member and exact BEST dependency, check regular single-link files, symlink-free ancestors, metadata/media/shot/classification/identity bindings, destination absence, same filesystem, index revisions and unexpected membership. Current preparation reads do not establish full PNG content integrity.
        2. **Recovery/storage:** establish and hash-verify a trustworthy recovery source separately. PLAYABLE USB disconnects mean it is NOT verified and cannot satisfy this gate. No backup/disk repair is part of Stage 2B.3c. Existing archive/journals/research are protected evidence, not a general recovery source. Reversible renames and before-images alone do not cover disk loss.
        3. **Quiescence:** terminate all standalone/in-process visualizers, MCP servers and CLI writers, generation/recheck/extraction jobs and index/vocabulary rebuilds; obtain existing catalog and registry exclusive locks as appropriate. Ordinary processes must remain stopped until complete activation or rollback. Do not attempt shared-to-exclusive upgrades. Freeze a unique operation/cohort ID, plan hash, registry snapshot and index hashes/revisions.
        4. **Prepare:** generate each transformed JSON from its frozen original with the stored schema/patch, check exact expected after SHA and unknown/semantic field equality, fsync before-images and staging directories. Preserve all original metadata bytes independently. Binaries are not copied as a second corpus. Prepare approved six corrections and allocator reservations separately; no IDs are guessed by the executor.
        5. **Journal:** use the retained SQLite movement conventions (FULL synchronous, bounded durable 128-file batches, expected hashes/size/mtime/owners, before/after paths, instance/mode/layout, errors/attempts/index generation). Stage states are planned -> prepared -> installed -> verified -> activated, with failed/recoverable interruptions. Record intention before mutation. Map owner publication states prepared/installing/files_published/committed/finished/rolled_back rather than replacing that journal.
        6. **Install under maintenance:** same-device renameat2 RENAME_NOREPLACE and fsync from the existing archival execution convention; NEVER overwrite/cross-device copy silently. Move PNG/array/text bytes losslessly. For JSON retain original before-images and install separately verified transformed bytes. Keep all batches unpublished; do not start applications after a subset. Use resource publication/adoption owners for location/layout/run state, not a new allocator or private pair publisher.
        7. **Verify:** independently freshly hash every installed destination and before-image, verify original namespace disposition and complete canonical/retained membership; resolve every reference and exact source identity/mode through the shared owners. Initial collection commitment is only after all members exist; array/mask/manifest controlled updates retain pairing. Capture all required current-location, source/run and publication revisions.
        8. **Registry/index cohort:** prepare an unpublished registry snapshot; preserve all identities/high-water/run rows. Build replacement Illustration indexes with the existing owner and a controlled staging output, context-aware vocabulary/stats/viability outputs through their owners. Old indexes remain intact. Compare all row IDs/paths/modes/lifecycle/shot types/navigation and revision metadata; SHA/fsync replacement files. The final indexed registry path must be stable, never a temporary adopting alias.
        9. **Coordinated activation:** while ALL processes remain stopped, install the completely verified registry and required index/revision files as one operator-controlled cohort. Multiple filesystem renames are NOT one atomic transaction: the maintenance barrier is mandatory. Only after all cohort hashes/revisions/reference smoke checks pass may explicit registry launch configuration be enabled. Missing six approvals, any file/reference failure, unready index or unverified recovery source prevents release. Never expose mixed layouts.
        10. **Application smoke:** new processes using the explicit registry: CLI annotation/shot and Illustration queries, both movie/gameplay navigation, Mosaic/Book copied assets, MCP active discovery/reference packets, per-mode engraving inspection, canonical read-only BEST/frame pairing, index freshness. Verify no repairs/writeback on reads. Generation is not a migration smoke test.
        11. **Recovery/rollback:** stop under maintenance. Reconcile physical source/destination hashes against the journal, not state labels. Original present/destination absent means retry preparation; original absent/exact after hash means verify before commit; both present/unexpected hash/collision means STOP, no overwrite. Restore JSON before-images and reverse verified binary renames with no-replace semantics, fsync, restore the matching registry/index/revision/configuration cohort, verify every rollback hash and reference before legacy restart. Keep new allocated IDs/run reservations in retained evidence; never reduce counters. Interrupted rollback uses the same observed-hash reconciliation. Restore application access only after the entire rollback cohort verifies.
        12. **Cleanup:** obsolete legacy location removal is a separate approved operation, not automatic migration completion. Keep archive, immutable evidence, before-images, old indexes and journals until retention/removal approval. No executable production cleanup command is included here.

        ## Measured Capacity And Estimates

        Additional conservative budget: {space['additional_bytes']:,} bytes; currently free: {free:,}. Metadata originals {before:,}; transformed JSON {after:,}; current plan {measured_plan_bytes:,}; proposal {proposal_bytes:,}; replacement-index budget {space['replacement_index_bytes']:,}; journal {space['journal_bytes']:,}; margin {space['margin_bytes']:,}. Registry staging/rollback allowance includes two times proposal+plan size. Existing old indexes are retained in place and do not require a second binary corpus. Current immutable binary bytes to rename: {binary_bytes:,}. Available inode count {space['available_inodes']:,}; conservative added inode bound {space['minimum_additional_inodes']:,}. All are measured logical bytes plus explicit growth allowances, not guaranteed allocated-size predictions; rerun space/inode and filesystem checks immediately before execution.

        Historical same-device archival rate was 1,661,512 files / 1,570.112 s (verified retained report). At that rate movement alone is approximately {space['future_move_seconds_historical_rate_estimate']:.0f} s; one binary hash pass at a conservative assumed 100 MiB/s is approximately {space['future_full_hash_seconds_estimate']:.0f} s. Metadata parsing, registry commits, fsync, index rebuilds and interruption recovery add substantial time; these are planning estimates, NOT a measured migration/rehearsal duration. Full rehearsal will measure throughput. No second full project copy is needed; no PLAYABLE operations occurred.
        """,
            "rehearsal.md": f"""# Full-Corpus Rehearsal Specification

        NOT RUN in Stage 2B.3c. Separate Stage 2B.3d approval is required. Scope: all {active:,} active records, {identities:,} prior IDs, six unresolved entries, {annotation_files} annotation artifacts, {engraving_files} engraving files, every file row and every reference. A representative sample is insufficient.

        Preparation entry: `python -m services.resource_migration --project PROJECT --output outputs/maintenance/media-first-stage-2b3c`. This writes only preparation outputs and refuses to overwrite an existing package. Full content traversal entry for separately approved Stage 2B.3d: Python `services.resource_migration.rehearsal(project, plan, approved=True)`. It has no move/activation/inference path. Do not invoke it now.

        1. Freeze package/proposal hashes and record operator approval. First resolve/approve all six before an ACCEPTED full release rehearsal; their Review entries remain traversed even while approval is pending. Allocate only via retained registry owner after classification approval and freeze its updated mapping without changing prior IDs.
        2. Traverse every source file in bounded SQLite order, freshly hash content and compare frozen size/mtime/ctime/inode/device/single-link checks, reject changed files/missing hashes, validate all destination parents/absence/collisions. Include every source/reference, not only stat samples. Check no unexpected members under the three relevant roots and compare current active index membership to the complete mapping.
        3. Validate every identity/key/shot high-water/source association and every deterministic path with data.media_id/resource_locations; preserve the two approved orthographic slug collisions as distinct instances. Check SQLite integrity/FKs/uniqueness and all adopted schema states, current commitments/revisions and pending operations.
        4. Reconstruct every proposed transformed JSON and compare expected after SHA/semantic equality. Walk every reference: relocation, explicit legacy alias, stale cache, missing/contradictory and immutable historical. Resolve before to the exact resource and after to the same proposed file/hash/instance. Recheck all collection artifact hashes and aliases, array/mask shape/dtype/count and stale-cache state through existing readers. Keep BEST indices and existing PNG bytes; do not decode or infer.
        5. Run isolated miniature projects through the actual adoption, publication, selected run, index builders/readers and recovery owners. Test each interruption boundary before/after rename, file verification, registry/layout CAS and index/cohort release. Reuse bounded temporary fixture storage; destroy it between cases. No second full project copy, hardlink clone or symlinked fake production root.
        6. Every engraving must match its exact source_instance_id/mode/current source hashes, immutable artifact hashes and canonical wrapper. Original historical run paths remain evidence only. Inspect actual BEST versus query-specific frame and retain non-equivalence disclosure. No new model/API/generation run is required.
        7. Build/verify required replacement index schemas through the existing owners in fixtures; define full staged production index builds as a later authorized install prerequisite because canonical files/commitments do not yet exist. Do not rebuild production indexes during a read-only rehearsal. Compare all expected identity/mode/lifecycle/navigation/shot-type/revision populations using the full mapping.
        8. Simulate journal resume, rollback conflicts and failed release; prove no partial activation with unresolved entries, partial verified counts, missing indexes, live writers or unverified recovery source. Retain a structured rehearsal report with all checked/failed/blocked counts, plan/input hashes, capacity/throughput observations, integrity and owner-test results.
        9. ACCEPTED requires every listed gate, zero unresolved active mappings/reference failures/collisions and operator acceptance. The code's `remaining_gates` field is deliberately explicit: a passing file traversal alone is NOT the full rehearsal. Do not run physical migration automatically after rehearsal.

        The plan is queryable and streaming; no million-row Python list, new registry, new run counter or generalized migration executor is introduced. The existing archival interruption conventions and ResourceStore publication journals supply durability and CAS semantics.
        """,
            "readiness.md": f"""# Stage 2B.3c Readiness

        Preparation accounting: {active:,} active silhouettes; {identities:,} retained immutable identities; {len(corrections)} explicit withheld records. Current active indexes: `{json.dumps(groups)}`. Annotation artifacts {annotation_files}; collections {collections}; engraving files {engraving_files}. All mapped/reviewed relevant current files {file_count:,}; dispositions `{json.dumps(counts)}`. Proposed targets use the shared approved owners, lowercase hyphen slugs, media-first movie/gameplay paths and mode-specific engraving filenames.

        Database integrity `{integrity}`, foreign-key errors {len(foreign_keys)}. Unique source/destination and identity/key constraints are enforced. Explicit planning issue counts: `{json.dumps(issues)}`; blocking issues {unsupported}. Missing/contradictory sources/references and collisions, if any, are retained in issues and Review, not silently discarded. Stale cache is not a repaired embedding. Archive/research/Book scopes are immutable/excluded with retained queryable inventory evidence; no historical census was repeated.

        Human decisions: approve the two exact setting:field patches; supply evidence-backed classifications for Big Jake and three Far And Away records. Do NOT assume setting; objects remains possible only with curator/source evidence. All six final IDs/destinations remain unset until approval. Existing 668,044 IDs, including Companeros object_0002 -> #0001, are unchanged. Source-frame decisions: `{json.dumps(metadata.get('source_frame_decisions', []))}`. Exact legacy project-root aliases are proposals, not automatic repairs; approval is required before their transformed metadata can be installed. Numeric frame markers are not required physical files. Any missing/contradictory remaining references stay blocked.

        Storage budget {space['additional_bytes']:,} additional bytes; currently free {free:,}; conservative capacity check {space['capacity_check']}. Exact assumptions/inode bounds and estimates are in execution-and-recovery.md and summary.json. PLAYABLE is NOT a verified recovery source. Recovery-source verification and execution approval are external prerequisites, not performed here.

        Stage 2B.3d may begin ONLY with separate human approval. Its complete rehearsal must resolve all mapping/reference/classification and owner-validation gates before acceptance. Physical migration is NOT ready/authorized merely because this preparation package or tests exist. Partial release is forbidden. Historical active resources must pass the existing source/classification/publication gates during rehearsal; no bulk gate bypass or implicit reclassification is authorized.

        No production data/index/preference rewrite, correction, adoption, upgrade, generation, deletion, archive mutation, backup operation, full rehearsal or migration was performed. Regression/safety results are recorded separately in verification.md; an unfinished test gate is not claimed passed.
        """,
            "verification.md": f"""# Verification

        Preparation SQLite integrity: {integrity}; foreign-key errors: {len(foreign_keys)}. Current active index membership and source ownership reconcile through explicit primary keys; source namespaces are freshly enumerated only for the active migration boundary. Source metadata/annotation/engraving/index SHA256 and size/mtime/ctime/inode/device/single-link fingerprints are stored per file; binary silhouettes have retained hash evidence plus fresh stat and require fresh full SHA256 in Stage 2B.3d. This is NOT complete fresh corpus content integrity or execution proof.

        Frozen input hashes: `{json.dumps(metadata['inputs'])}`. Preparation time: {metadata['preparation_seconds']:.2f}s. No model/API/decoder ran. No production registry/index was created or activated. Original archive/research outputs remain read-only evidence; output writes are confined to this package.

        Focused tests and full regression gate: PENDING final result recording. Required unchanged command: `QT_QPA_PLATFORM=offscreen PYTHONFAULTHANDLER=1 uv run pytest tests/ -q --ignore=tests/test_shot_visualizer_framework.py --tb=short`. Baseline 2540 passed + 3 subtests. No new exclusions or success-by-retry policy.

        Known limitations: four classification ambiguities; six approval/allocation dependencies; stale cache hashes remain stale; missing/contradictory active references, if found, are explicit blockers; BEST pixel/query equivalence not retrospectively certified; external recovery source not verified; no full-corpus rehearsal, production adoption, index rebuild, installation or rollback has occurred. See issue counts in summary.json and exact issues in SQLite.
        """
    }
    for name, text in reports.items():
        atomic_write_text(output / name, inspect.cleandoc(text) + "\n")


def recovery_action(state, source_hash, destination_hash, before_hash, after_hash):
    if state not in ("planned", "prepared", "installed", "verified", "activated", "failed"):
        raise ValueError("Unknown migration journal state")
    if source_hash == before_hash and destination_hash is None:
        return "prepare-or-retry" if state != "activated" else "maintenance-recovery-required"
    if source_hash is None and destination_hash == after_hash:
        return "verify-before-commit" if state != "activated" else "verify-active-cohort"
    return "stop-conflict-no-overwrite"


def activation_ready(*, planned, verified, unresolved, failures, indexes_ready, recovery_source_verified, writers_quiescent):
    if type(planned) is not int or planned < 1 or type(verified) is not int or verified < 0:
        return False
    return (planned == verified and unresolved == 0 and failures == 0 and indexes_ready
            and recovery_source_verified and writers_quiescent)


def _schema(database):
    database.executescript("""
    PRAGMA foreign_keys=ON;
    PRAGMA synchronous=FULL;
    PRAGMA cache_size=-32768;
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE identity_bindings(registration_key TEXT PRIMARY KEY,instance_id TEXT UNIQUE,shot_id TEXT,instance_number INTEGER,
      media_type TEXT,media_id TEXT,legacy_identity TEXT,legacy_json TEXT UNIQUE,legacy_png TEXT,registration_state TEXT,created_at TEXT);
    CREATE TABLE shot_reservations(shot_id TEXT PRIMARY KEY,high_water INTEGER NOT NULL);
    CREATE TABLE silhouettes(legacy_json TEXT PRIMARY KEY,legacy_png TEXT,registration_key TEXT,instance_id TEXT UNIQUE,
      media_type TEXT,media_id TEXT,shot_id TEXT,media_item TEXT,field TEXT,label TEXT,slug TEXT,destination TEXT UNIQUE,status TEXT);
    CREATE TABLE files(source TEXT PRIMARY KEY,destination TEXT UNIQUE,disposition TEXT NOT NULL CHECK(disposition IN ('Move','Transform','Rebuild','Retain','Review','Exclude')),
      owner TEXT,instance_id TEXT REFERENCES identity_bindings(instance_id),mode TEXT,size INTEGER,mtime_ns INTEGER,ctime_ns INTEGER,
      inode INTEGER,device INTEGER,links INTEGER,sha256 TEXT,hash_evidence TEXT,reason TEXT);
    CREATE TABLE transformations(source TEXT PRIMARY KEY REFERENCES files(source),schema TEXT,patch TEXT,after_sha256 TEXT,after_bytes INTEGER);
    CREATE TABLE reference_plan(source TEXT,pointer TEXT,target TEXT,proposed_target TEXT,classification TEXT,required INTEGER,
      PRIMARY KEY(source,pointer,target));
    CREATE TABLE collections(media_type TEXT,media_id TEXT,filename TEXT,artifacts TEXT,stale_manifests INTEGER,PRIMARY KEY(media_type,media_id));
    CREATE TABLE corrections(reference TEXT PRIMARY KEY,payload TEXT NOT NULL);
    CREATE TABLE issues(category TEXT,source TEXT,detail TEXT,blocking INTEGER NOT NULL);
    CREATE TABLE index_transitions(source TEXT PRIMARY KEY,owner TEXT,action TEXT,activation_gate TEXT);
    CREATE TABLE inventory_scopes(scope TEXT PRIMARY KEY,disposition TEXT,evidence TEXT,freshly_enumerated INTEGER);
    CREATE VIEW silhouette_files AS SELECT legacy_json source,destination||'/silhouette.json' destination,instance_id,status FROM silhouettes
      UNION ALL SELECT legacy_png,destination||'/silhouette.png',instance_id,status FROM silhouettes;
    """)


def _issue(database, category, source, detail, blocking=True):
    database.execute("INSERT INTO issues VALUES(?,?,?,?)", (category, str(source), str(detail), int(blocking)))


def _file(database, project, source, destination, disposition, owner, *, instance_id=None, mode="", hash_bytes=False, reason="", baseline=None):
    from data.resource_storage import digest
    source = str(source)
    try:
        path = locations.project_file(project, source)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Expected regular single-link source")
        relative_destination = None
        if destination is not None:
            target = Path(destination)
            target = target if target.is_absolute() else project / target
            relative_destination = str(target.relative_to(project))
            if ".." in target.parts:
                raise ValueError("Escaping destination")
            for parent in (target, *target.parents):
                if parent == project:
                    break
                if parent.is_symlink():
                    raise ValueError("Symlink destination ancestry")
            if target.exists():
                raise ValueError("Destination already exists; never overwrite")
            existing_parent = target.parent
            while not existing_parent.exists():
                existing_parent = existing_parent.parent
            if existing_parent.stat().st_dev != info.st_dev:
                raise ValueError("Cross-filesystem move requires separately approved copy strategy")
        previous = baseline.execute("SELECT size,mtime_ns,sha256 FROM files WHERE path=?", (source,)).fetchone() if baseline else None
        current_hash = digest(path) if hash_bytes else previous[2] if previous else None
        if previous and (previous[0] != info.st_size or previous[1] != info.st_mtime_ns):
            _issue(database, "source-drift", source, "Source stat differs from retained research; current row recorded, requires review")
        if hash_bytes and previous and previous[2] and previous[2] != current_hash:
            _issue(database, "source-drift", source, "Fresh metadata hash differs from retained research")
        database.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (source, relative_destination, disposition, owner,
                         instance_id, mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev, info.st_nlink,
                         current_hash, "fresh SHA256" if hash_bytes else "retained SHA256 plus fresh stat" if current_hash else "fresh stat; rehearsal SHA256 required", reason))
        return path
    except (OSError, ValueError, sqlite3.IntegrityError) as exc:
        _issue(database, "file-preflight", source, exc)
        database.execute("INSERT OR IGNORE INTO files(source,disposition,owner,instance_id,mode,reason) VALUES(?,'Review',?,?,?,?)",
                         (source, owner, instance_id, mode, str(exc)))
        return None


def _transform(database, source, schema, result, *, patch=None):
    payload = encode(result)
    database.execute("INSERT INTO transformations VALUES(?,?,?,?,?)", (source, schema, json.dumps(result if patch is None else patch, ensure_ascii=False), sha256(payload), len(payload)))


def _reference(database, source, pointer, target, proposed, classification, required=True):
    if isinstance(target, str) and target:
        database.execute("INSERT OR REPLACE INTO reference_plan VALUES(?,?,?,?,?,?)", (source, pointer, target, proposed, classification, int(required)))


def prepare_project(project, output):
    """Reconcile retained mappings and active sources; write only the approved output directory."""
    from data.media_id import label_slug
    from data.resource_storage import digest
    project = Path(project).resolve(strict=True)
    output = Path(output)
    output = output if output.is_absolute() else project / output
    if output.relative_to(project).parts[:2] != ("outputs", "maintenance") or ".." in output.parts:
        raise ValueError("Preparation output must be under project outputs/maintenance")
    for parent in (output, *output.parents):
        if parent == project:
            break
        if parent.is_symlink():
            raise ValueError("Preparation output symlink")
    output.mkdir(parents=True, exist_ok=True)
    plan_path = output / "migration-plan.sqlite3"
    if plan_path.exists():
        raise ValueError("Existing preparation package must be preserved; inspect or explicitly select a new output")
    started = time.monotonic()
    research_root = project / "outputs/tests/media-first-migration"
    proposal_path = project / "outputs/tests/silhouette-canonical-identity/registry-proposal.sqlite3"
    adoption = registry_adoption_plan(proposal_path)
    if adoption["project"] != str(project):
        raise ValueError("Proposal project differs from preparation project")
    inputs = {str(path.relative_to(project)): digest(path) for path in (proposal_path, research_root / "migration-plan.sqlite3")}
    media_cache = {}
    def media_record(media_type, media_id):
        key = media_type, media_id
        if key not in media_cache:
            media_cache[key] = locations.find_media(project, media_type, media_id=media_id)
        return media_cache[key]
    with sqlite3.connect(plan_path) as database, readonly(research_root / "migration-plan.sqlite3") as research, readonly(proposal_path) as proposal, readonly(research_root / "safety-final.sqlite3") as baseline:
        database.row_factory = sqlite3.Row
        _schema(database)
        database.executemany("INSERT INTO identity_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?)", (tuple(row) for row in proposal.execute("SELECT * FROM instances ORDER BY registration_key")))
        database.executemany("INSERT INTO shot_reservations VALUES(?,?)", proposal.execute("SELECT * FROM shots"))
        database.executemany("INSERT INTO silhouettes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (tuple(row) for row in research.execute("SELECT * FROM silhouettes ORDER BY legacy_json")))
        database.commit()
        print("Identity mappings retained; preparing annotation collections", flush=True)
        annotation_rows = list(research.execute("SELECT * FROM annotations ORDER BY source"))
        path_mapping = {row["source"]: row["destination"] for row in annotation_rows}
        aliases = {}
        collection_artifacts = {}
        for row in annotation_rows:
            source = row["source"]
            suffix = next((value for value in locations.ANNOTATION_SUFFIXES if source.endswith(value)), None)
            media = media_record(row["media_type"], research.execute("SELECT media_id FROM media WHERE media_type=? AND filename_stem=?", (row["media_type"], row["filename_stem"])).fetchone()[0])
            target = locations.annotation_destination(project, media, row["media_type"], suffix) if suffix else None
            if target is None or str(target.relative_to(project)) != row["destination"]:
                _issue(database, "annotation-owner", source, "Research mapping differs from the shared artifact owner")
            path = _file(database, project, source, target, "Transform" if source.endswith(".manifest.json") else "Move", "data.resource_locations / data.resource_storage", hash_bytes=True, baseline=baseline)
            if path and suffix:
                collection_artifacts.setdefault((row["media_type"], media["media_id"], media["filename"]), {})[suffix] = {"source": source, "destination": row["destination"], "sha256": digest(path)}
                if suffix in (".annotations.json", ".annotations.txt", ".annotations.npy"):
                    alias = str(Path(source).with_name(row["filename_stem"] + suffix.replace(".annotations", "")))
                    aliases[alias] = source
        for row in annotation_rows:
            if not row["source"].endswith(".manifest.json"):
                continue
            source = row["source"]
            manifest = json.loads((project / source).read_text())
            mapping = {**path_mapping, **{alias: path_mapping[actual] for alias, actual in aliases.items()}}
            try:
                result = transform_manifest(manifest, mapping, legacy_aliases=aliases)
                _transform(database, source, "manifest-v1", result)
                for section in ("json", "txt", "npy", "valid"):
                    descriptor = manifest.get(section)
                    if descriptor is None:
                        continue
                    original = descriptor["path"]
                    actual = aliases.get(original, original)
                    current = database.execute("SELECT sha256 FROM files WHERE source=?", (actual,)).fetchone()
                    classification = "legacy alias" if original in aliases else "path relocation"
                    if current is None:
                        classification = "missing or contradictory reference"
                        _issue(database, "manifest-reference", source, original)
                    elif descriptor.get("hash") != "sha256:" + str(current[0]):
                        classification = "existing stale cache"
                        _issue(database, "stale-cache", source, section + ": recorded hash differs; preserve hash and bytes, do not bless cache", False)
                    _reference(database, source, "/" + section + "/path", original, result[section]["path"], classification)
            except (ValueError, KeyError, TypeError) as exc:
                _issue(database, "manifest-schema", source, exc)
                database.execute("UPDATE files SET disposition='Review',reason=? WHERE source=?", (str(exc), source))
        for key, artifacts in collection_artifacts.items():
            stale = database.execute("SELECT count(DISTINCT source) FROM issues WHERE category='stale-cache' AND source LIKE ?", (str(Path(next(iter(artifacts.values()))["source"]).parent / (Path(key[2]).stem + "%")),)).fetchone()[0]
            database.execute("INSERT INTO collections VALUES(?,?,?,?,?)", (*key, json.dumps(artifacts), stale))
        database.commit()
        print("Preparing active silhouette pairs, without allocation or PNG hashing", flush=True)
        for number, row in enumerate(database.execute("SELECT * FROM silhouettes ORDER BY legacy_json"), 1):
            instance_id = row["instance_id"]
            metadata_target = image_target = None
            if instance_id:
                media = media_record(row["media_type"], row["media_id"])
                pair = locations.silhouette_destination(project, media, row["media_type"], instance_id, row["field"], row["label"])
                metadata_target, image_target = pair["metadata"], pair["image"]
                if str(pair["dir"].relative_to(project)) != row["destination"] or label_slug(row["label"]) != row["slug"]:
                    _issue(database, "destination-owner", row["legacy_json"], "Retained destination differs from the approved shared owner")
                binding = database.execute("SELECT * FROM identity_bindings WHERE instance_id=?", (instance_id,)).fetchone()
                if binding is None or any(binding[key] != row[key] for key in ("registration_key", "media_type", "media_id", "shot_id", "legacy_json", "legacy_png")):
                    _issue(database, "identity-binding", row["legacy_json"], "Research mapping differs from the immutable proposal")
            metadata_path = _file(database, project, row["legacy_json"], metadata_target, "Transform" if instance_id else "Review", "services.resource_publication", instance_id=instance_id, hash_bytes=True, baseline=baseline, reason="missing field; no ID allocated" if not instance_id else "")
            _file(database, project, row["legacy_png"], image_target, "Move" if instance_id else "Review", "services.resource_publication", instance_id=instance_id, baseline=baseline, reason="missing field; no ID allocated" if not instance_id else "")
            if metadata_path:
                record = json.loads(metadata_path.read_text())
                if any(record.get(key) != row[key] for key in ("media_type", "media_id", "shot_id", "field", "label")):
                    _issue(database, "metadata-binding", row["legacy_json"], "Current JSON differs from the retained indexed identity/classification")
                if instance_id:
                    try:
                        transformed = transform_silhouette(record, instance_id)
                        _transform(database, row["legacy_json"], "silhouette-v1", transformed, patch={key: transformed[key] for key in ("publication_contract", "resource_kind", "instance_id", "png")})
                    except (ValueError, TypeError) as exc:
                        _issue(database, "silhouette-schema", row["legacy_json"], exc)
                else:
                    correction = correction_proposal(project, row["legacy_json"])
                    database.execute("INSERT INTO corrections VALUES(?,?)", (row["legacy_json"], json.dumps(correction, ensure_ascii=False)))
                _reference(database, row["legacy_json"], "/png", row["legacy_png"], str(image_target.relative_to(project)) if image_target else None, "path relocation" if instance_id else "classification approval required")
                _reference(database, row["legacy_json"], "/source_frame", record.get("source_frame"), record.get("source_frame"), "retained BEST materialization; not the query-specific frame selection")
            if number % 4096 == 0:
                database.commit()
            if number % 100000 == 0:
                print(f"Prepared {number:,} active records in {time.monotonic() - started:.1f}s", flush=True)
        database.commit()
        _prepare_engravings(database, project, research, baseline, media_record)
        _prepare_remaining(database, project, research, baseline)
        _reconcile_indexes(database, project)
        _check_references(database, project)
        _check_membership(database, project)
        database.execute("CREATE INDEX reference_target ON reference_plan(target)")
        database.execute("CREATE INDEX issues_category ON issues(category)")
        for key, value in {"stage": "2B.3c", "project": str(project), "inputs": inputs, "adoption": adoption,
                           "preparation_seconds": time.monotonic() - started, "rehearsal_performed": False, "production_mutation": False}.items():
            database.execute("INSERT INTO meta VALUES(?,?)", (key, json.dumps(value)))
        database.commit()
        integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = list(database.execute("PRAGMA foreign_key_check"))
        if integrity != "ok" or foreign_keys:
            raise ValueError("Preparation database integrity failure")
    _write_reports(project, output)
    return {"plan": str(plan_path), "preparation_seconds": time.monotonic() - started}


def resume_preparation(project, output):
    """Finish a retained annotation/silhouette checkpoint without repeating its inventory."""
    from data.resource_storage import digest
    project = Path(project).resolve(strict=True)
    output = Path(output)
    output = output if output.is_absolute() else project / output
    if output.relative_to(project).parts[:2] != ("outputs", "maintenance") or ".." in output.parts:
        raise ValueError("Resume output must be below outputs/maintenance")
    plan_path = locations.project_file(project, output / "migration-plan.sqlite3")
    research_root = project / "outputs/tests/media-first-migration"
    proposal_path = project / "outputs/tests/silhouette-canonical-identity/registry-proposal.sqlite3"
    adoption = registry_adoption_plan(proposal_path)
    started = time.monotonic()
    with sqlite3.connect(plan_path) as database, readonly(research_root / "migration-plan.sqlite3") as research, readonly(research_root / "safety-final.sqlite3") as baseline:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA synchronous=FULL")
        database.execute("PRAGMA cache_size=-32768")
        count = database.execute("SELECT count(*) FROM silhouettes").fetchone()[0]
        if (count != research.execute("SELECT count(*) FROM silhouettes").fetchone()[0]
                or database.execute("SELECT count(*) FROM files WHERE source LIKE 'data/silhouettes/%'").fetchone()[0] != count * 2
                or database.execute("SELECT count(*) FROM identity_bindings").fetchone()[0] != adoption["identities"]):
            raise ValueError("Incomplete silhouette checkpoint; refuse implicit replay")
        if (database.execute("SELECT 1 FROM files WHERE source LIKE 'data/engravings/%' LIMIT 1").fetchone()
                or database.execute("SELECT 1 FROM index_transitions LIMIT 1").fetchone()
                or database.execute("SELECT 1 FROM sqlite_master WHERE name='active_index_membership'").fetchone()):
            raise ValueError("Later preparation phase already exists; refuse overwriting it")
        inputs = {str(path.relative_to(project)): digest(path) for path in (proposal_path, research_root / "migration-plan.sqlite3")}
        media_cache = {}
        def media_record(media_type, media_id):
            key = media_type, media_id
            if key not in media_cache:
                media_cache[key] = locations.find_media(project, media_type, media_id=media_id)
            return media_cache[key]
        _prepare_engravings(database, project, research, baseline, media_record)
        _prepare_remaining(database, project, research, baseline)
        _reconcile_indexes(database, project)
        _check_references(database, project)
        _check_membership(database, project)
        database.execute("CREATE INDEX reference_target ON reference_plan(target)")
        database.execute("CREATE INDEX issues_category ON issues(category)")
        values = {"stage": "2B.3c", "project": str(project), "inputs": inputs, "adoption": adoption,
                  "preparation_seconds": time.monotonic() - started, "resumed_completed_annotation_silhouette_checkpoint": True,
                  "rehearsal_performed": False, "production_mutation": False}
        for key, value in values.items():
            database.execute("INSERT INTO meta VALUES(?,?)", (key, json.dumps(value)))
        database.commit()
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or database.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Resumed preparation integrity failure")
    _write_reports(project, output)
    return {"plan": str(plan_path), "resumed": True, "remaining_preparation_seconds": time.monotonic() - started}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    print(json.dumps(prepare_project(arguments.project, arguments.output), indent=2))


if __name__ == "__main__":
    main()