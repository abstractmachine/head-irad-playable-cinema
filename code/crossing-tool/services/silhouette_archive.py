"""Read-only superseded archival planning, separate from identity migration."""

import hashlib
import json
import os
import re
import resource
import sqlite3
import stat
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path, PurePosixPath


def archive_path(original: str) -> str:
    path = PurePosixPath(original)
    if path.is_absolute() or ".." in path.parts or "\\" in original or path.parts[:2] != ("data", "silhouettes") or len(path.parts) < 3:
        raise ValueError("Source must be a safe relative path below data/silhouettes")
    return PurePosixPath("archive", "silhouettes", *path.parts[2:]).as_posix()


def selected(metadata: dict) -> bool:
    from services.silhouette_catalog import assignment_state_for_record
    return assignment_state_for_record(metadata) == "superseded"


def eligibility(reasons: list[str], historical_references: int = 0) -> str:
    if "shared_with_active" in reasons:
        return "C_shared_active_blocking"
    if any(reason.startswith("active_dependency") for reason in reasons):
        return "B_active_dependency_blocking"
    if reasons:
        return "D_needs_review"
    if historical_references:
        return "E_historical_dependency"
    return "A_safe_to_archive"


def _artifact(path, value):
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


@lru_cache(maxsize=4096)
def _parent_is_symlink(path):
    return path.is_symlink()


def _safe_source(project, relative):
    archive_path(relative)
    path = project / relative
    if path.is_symlink() or any(_parent_is_symlink(parent) for parent in path.parents if parent != project and parent.is_relative_to(project)):
        raise ValueError("Symlinked source cannot be archived")
    return path


def _critical(relative):
    return relative.startswith(("data/annotations/", "data/engravings/", "data/indexes/illustration/", "outputs/books/", "outputs/tests/engraving-benchmark/")) or "/Companeros (1970) {tmdb-61044}/bird/object_0002." in relative


def _snapshot(project, database, output, phase):
    started = time.perf_counter()
    changed = []
    checked = 0
    rehashed = 0
    manifest = output / f"safety-{phase}-files.jsonl"
    with manifest.open("w", encoding="utf-8") as handle:
        for relative, size, mtime, sha256, kind in database.execute("SELECT path,size,mtime_ns,sha256,kind FROM census.files ORDER BY path"):
            path = project / relative
            reasons = []
            digest = None
            observed = None
            try:
                info = path.lstat()
                current_kind = "symlink" if stat.S_ISLNK(info.st_mode) else "regular" if stat.S_ISREG(info.st_mode) else "special"
                observed = {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "kind": current_kind}
                if (info.st_size, info.st_mtime_ns, current_kind) != (size, mtime, kind):
                    reasons.append("differs_from_census_metadata")
                if _critical(relative) and current_kind == "regular":
                    hasher = hashlib.sha256()
                    with path.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            hasher.update(chunk)
                    digest = hasher.hexdigest()
                    rehashed += 1
                    if digest != sha256:
                        reasons.append("differs_from_census_sha256")
                    after = path.lstat()
                    if (after.st_size, after.st_mtime_ns, after.st_ino) != (info.st_size, info.st_mtime_ns, info.st_ino):
                        reasons.append("changed_while_hashing")
            except OSError as exc:
                reasons.append(f"unavailable:{type(exc).__name__}")
            if reasons:
                changed.append({"path": relative, "reasons": reasons})
            handle.write(json.dumps({"path": relative, "method": "fresh_sha256_and_metadata" if digest else "metadata_compared_to_verified_census", "observed": observed, "sha256": digest, "baseline_sha256": sha256, "issues": reasons}, separators=(",", ":")) + "\n")
            checked += 1
            if checked % 500000 == 0:
                print(f"Archive safety {phase}: {checked:,} baseline files checked", flush=True)
    registry = project / "data/silhouettes/.legacy-ordinal-reservations.sqlite3"
    registry_observed = None
    if registry.is_file() and not registry.is_symlink():
        registry_observed = {"path": registry.relative_to(project).as_posix(), "size": registry.stat().st_size, "mtime_ns": registry.stat().st_mtime_ns, "sha256": hashlib.sha256(registry.read_bytes()).hexdigest()}
    result = {"files_checked": checked, "freshly_rehashed_files": rehashed, "metadata_only_files": checked - rehashed, "authorized_registry": registry_observed, "changes_since_census": changed, "seconds": time.perf_counter() - started, "per_file_evidence": manifest.name, "limitations": "Metadata-only comparisons are not fresh byte verification. Existing census SHA256 remains provenance; atime is excluded. No repeated production-tree traversal; newly added files outside census are not enumerated."}
    _artifact(output / f"safety-{phase}.json", result)
    return result


def compare_snapshots(output, before="before", after="after", *, baseline_output=None):
    changes = []
    limitations = 0
    count = 0
    with ((baseline_output or output) / f"safety-{before}-files.jsonl").open() as first, (output / f"safety-{after}-files.jsonl").open() as second:
        for left_line, right_line in zip(first, second, strict=True):
            left, right = json.loads(left_line), json.loads(right_line)
            if left["path"] != right["path"]:
                raise ValueError("Safety snapshot paths differ")
            if "observed" not in left or "observed" not in right:
                limitations += 1
            if (left.get("observed"), left["sha256"], left["issues"]) != (right.get("observed"), right["sha256"], right["issues"]):
                changes.append({"path": left["path"], "before": left, "after": right})
            count += 1
    first_summary = (baseline_output or output) / f"safety-{before}.json"
    second_summary = output / f"safety-{after}.json"
    registry_transition = None
    if first_summary.is_file() and second_summary.is_file():
        old = json.loads(first_summary.read_text()).get("authorized_registry")
        new = json.loads(second_summary.read_text()).get("authorized_registry")
        registry_transition = {"before": old, "after": new}
        if baseline_output is None and old != new:
            changes.append({"path": "data/silhouettes/.legacy-ordinal-reservations.sqlite3", "before": old, "after": new})
    result = {"files_compared": count, "changed_during_comparison": changes, "authorized_registry_transition": registry_transition, "files_without_saved_current_metadata": limitations, "zero_detected_task_changes": not changes, "snapshot_pair": [before, after], "limitations": "Fresh hashes cover critical products only; other files compare metadata. The authorized new registry is separately hashed. Old snapshots without observed metadata cannot prove equal current mtimes for census-mismatching paths."}
    _artifact(output / f"safety-comparison-{before}-{after}.json", result)
    return result


def verify_namespace_counts(project, database):
    expected = dict(database.execute("SELECT domain,count(*) FROM census.files GROUP BY domain"))
    actual = Counter()
    excluded = {project / "outputs/tests/silhouette-identity-migration", project / "outputs/tests/silhouette-archive"}
    stack = [project / name for name in ("data", "outputs", "preferences") if (project / name).is_dir()]
    while stack:
        directory = stack.pop()
        if directory in excluded:
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                else:
                    if path == project / "data/silhouettes/.legacy-ordinal-reservations.sqlite3":
                        continue
                    actual["/".join(path.relative_to(project).parts[:2])] += 1
    changes = {domain: {"census": expected.get(domain, 0), "current": actual.get(domain, 0)} for domain in sorted(set(expected) | set(actual)) if expected.get(domain, 0) != actual.get(domain, 0)}
    return {"baseline_files": sum(expected.values()), "current_files": sum(actual.values()), "domain_count_differences": changes, "authorized_additional_registry": (project / "data/silhouettes/.legacy-ordinal-reservations.sqlite3").is_file(), "method": "One no-decode scandir pass, plus before/after existence checks for every census path; both isolated research directories and the separately hashed authorized registry excluded."}


def allocator_reservations(database, output):
    affected = 0
    archived = 0
    namespaces = 0
    query = """SELECT substr(metadata_path,1,length(metadata_path)-length(object_id)-5) AS namespace,
        max(CAST(substr(object_id,8) AS INTEGER)) AS preserved_max,
        max(CASE WHEN assignment!='superseded' THEN CAST(substr(object_id,8) AS INTEGER) ELSE 0 END) AS remaining_max,
        sum(CASE WHEN assignment='superseded' THEN 1 ELSE 0 END) AS archived_records
        FROM census.records GROUP BY namespace HAVING archived_records>0 ORDER BY namespace"""
    with (output / "legacy-ordinal-reservations.jsonl").open("w") as handle:
        for namespace, preserved, remaining, count in database.execute(query):
            namespaces += 1
            risk = preserved > remaining
            affected += risk
            archived += count if risk else 0
            handle.write(json.dumps({"original_namespace": namespace.rstrip("/"), "maximum_preserved_ordinal": preserved, "maximum_remaining_ordinal": remaining, "next_ordinal_must_exceed": preserved, "allocator_reuse_risk": risk, "archived_records": count}, separators=(",", ":")) + "\n")
    return {"namespaces_at_risk": affected, "historical_records_in_at_risk_namespaces": archived, "namespaces_with_historical_records": namespaces, "implemented": True, "production_registry_initialized_by_this_run": False, "production_deployment_verified": False, "requirement": "Fixture-tested high-water mechanics reserve batch ranges atomically. Approved initialization must preserve every historical namespace before archival; production deployment is not verified and this dry run never initializes it."}


def _read_candidates(project, database, summary):
    from services.silhouette_catalog import _safe_label
    candidates = database.execute("SELECT metadata_path,png_path,media_type,filename,shot_id,field,label,object_id FROM census.records WHERE assignment='superseded' ORDER BY metadata_path")
    for number, row in enumerate(candidates, 1):
        metadata_path, png_path, media, filename, shot, field, label, object_id = row
        reasons = []
        metadata = {}
        checksum = None
        try:
            raw = _safe_source(project, metadata_path).read_bytes()
            checksum = hashlib.sha256(raw).hexdigest()
            metadata = json.loads(raw)
            if not isinstance(metadata, dict):
                raise ValueError("Metadata must be an object")
            if not selected(metadata):
                summary["census_candidates_no_longer_superseded"] += 1
                continue
            live_png = metadata.get("png")
            if not isinstance(live_png, str) or Path(live_png).name != live_png or "\\" in live_png:
                reasons.append("ambiguous_png_reference")
            else:
                png_path = (PurePosixPath(metadata_path).parent / live_png).as_posix()
            summary["authoritative_superseded_confirmed"] += 1
        except (OSError, ValueError, TypeError) as exc:
            reasons.append(f"metadata_unverified:{type(exc).__name__}")
        assignment = metadata.get("assignment") or {}
        recheck = assignment.get("recheck") or {}
        replacements = assignment.get("superseded_by", recheck.get("replacement_objects", []))
        replacements = [replacements] if isinstance(replacements, dict) else replacements if isinstance(replacements, list) else []
        cause = "source_job_recheck" if recheck.get("source_job") else "identity_recheck" if recheck else "unspecified_retirement"
        summary["causes"][cause] += 1
        summary["recheck_results"][str(recheck.get("result") or "not_recorded")] += 1
        provenance = metadata.get("search_provenance") or {}
        summary["provenance_states"][str(provenance.get("state") or "not_recorded")] += 1
        summary["provenance_methods"][str(provenance.get("method") or "not_recorded")] += 1
        values = recheck.get("annotation_values") or []
        canonical_label = str(metadata.get("label", label)).casefold().strip()
        phrases = [value.casefold().strip() for value in values if isinstance(value, str)]
        if canonical_label not in phrases:
            summary["historical_label_not_exact_recheck_value"] += 1
        if canonical_label not in phrases and any(canonical_label in phrase.split() and len(phrase.split()) > 1 for phrase in phrases):
            summary["possible_atomic_fragment_of_compound_value"] += 1
            if len(summary["compound_examples"]) < 12:
                summary["compound_examples"].append({"metadata_path": metadata_path, "label": canonical_label, "current_recheck_values": values})
        database.execute("INSERT INTO chosen VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (metadata_path, png_path, media, filename, shot, field, label, object_id, json.dumps(replacements), json.dumps(reasons), checksum, json.dumps({key: metadata[key] for key in ("source_frame",) if key in metadata})))
        for target in replacements:
            if isinstance(target, dict) and all(target.get(key) for key in ("media_type", "filename_stem", "label", "object_id")):
                target_path = f"data/silhouettes/catalog/{target['media_type']}/{target['filename_stem']}/{_safe_label(str(target['label']))}/{target['object_id']}.json"
                database.execute("INSERT OR IGNORE INTO replacements VALUES(?,?)", (metadata_path, target_path))
            else:
                database.execute("INSERT OR IGNORE INTO replacements VALUES(?,?)", (metadata_path, "!unresolved:" + json.dumps(target)))
        if number % 25000 == 0:
            database.commit()
        if number % 100000 == 0:
            print(f"Archive selection: {number:,} authoritative candidates read", flush=True)
    database.commit()


def _build_assets(project, database):
    database.executescript("""
    INSERT INTO record_files SELECT metadata_path,metadata_path FROM chosen;
    INSERT OR IGNORE INTO record_files SELECT metadata_path,png_path FROM chosen WHERE png_path IS NOT NULL;
    CREATE INDEX chosen_png ON chosen(png_path);
    """)
    for relative, in database.execute("SELECT path FROM census.files WHERE domain='data/silhouettes' AND ((path NOT LIKE '%.png' AND path NOT LIKE '%.json') OR path GLOB '*/object_*.*.*')"):
        path = PurePosixPath(relative)
        stem = path.name.split(".", 1)[0]
        if re.fullmatch(r"object_\d+", stem):
            metadata = (path.parent / (stem + ".json")).as_posix()
            database.execute("INSERT OR IGNORE INTO record_files SELECT metadata_path,? FROM chosen WHERE metadata_path=?", (relative, metadata))
    database.executescript("""
    CREATE INDEX record_files_path ON record_files(path);
    INSERT INTO assets(path,size,sha256,mtime_ns,kind)
      SELECT DISTINCT rf.path,f.size,f.sha256,f.mtime_ns,f.kind FROM record_files rf LEFT JOIN census.files f ON f.path=rf.path;
    UPDATE assets SET shared_active=1 WHERE EXISTS(SELECT 1 FROM census.records r WHERE r.assignment='active' AND (r.png_path=assets.path OR r.metadata_path=assets.path));
    INSERT INTO source_states SELECT metadata_path,metadata_path,assignment FROM census.records;
    INSERT OR IGNORE INTO source_states SELECT index_path || '#records[' || row_identity || ']',metadata_path,assignment FROM census.indexed;
    """)
    return _validate_assets(project, database)


def _validate_assets(project, database):
    devices = Counter()
    allocated_bytes = 0
    hardlinked = 0
    archive_root = project / "archive"
    archive_present = os.path.lexists(archive_root)
    for path, size, sha256, mtime, kind, shared, _, _ in database.execute("SELECT * FROM assets ORDER BY path"):
        reasons = []
        try:
            source = _safe_source(project, path)
            info = source.lstat()
            devices[info.st_dev] += 1
            allocated_bytes += getattr(info, "st_blocks", 0) * 512
            if info.st_nlink > 1:
                hardlinked += 1
                reasons.append("hardlinked_source_needs_review")
            if not stat.S_ISREG(info.st_mode):
                reasons.append("nonregular_source")
            if size is None or (size, mtime) != (info.st_size, info.st_mtime_ns):
                reasons.append("source_metadata_differs_from_baseline")
        except (OSError, ValueError):
            reasons.append("missing_or_unsafe_source")
        try:
            destination = archive_path(path)
            if archive_present and os.path.lexists(project / destination):
                reasons.append("destination_collision")
            if archive_present and any(parent.is_symlink() for parent in (project / destination, *(project / destination).parents) if parent.is_relative_to(project) and parent != project):
                reasons.append("symlinked_archive_destination")
            if archive_present and not archive_root.is_dir():
                reasons.append("non_directory_archive_ancestor")
        except ValueError:
            destination = None
            reasons.append("unsafe_archive_path")
        if sha256 is None:
            reasons.append("checksum_unavailable")
        if shared:
            reasons.append("shared_with_active")
        database.execute("UPDATE assets SET destination=?,reasons=? WHERE path=?", (destination, json.dumps(reasons), path))
    database.commit()
    return {"source_filesystem_devices": dict(devices), "allocated_storage_bytes": allocated_bytes, "hardlinked_source_files": hardlinked}


def _dependencies(database):
    database.executescript("""
    CREATE TABLE dependencies AS
    SELECT a.path AS target,
     CASE WHEN ss.state IN ('active','inactive') AND r.kind='identity_reconstructible'
             AND (r.pointer='/assignment/supersedes' OR r.pointer LIKE '/assignment/supersedes/%')
         THEN 'historical_lineage_resolvable'
         WHEN ss.state='active' THEN 'active_silhouette'
           WHEN ss.state='inactive' THEN 'inactive_retained_data'
           WHEN ss.state='superseded' THEN 'historical_silhouette'
           WHEN r.subsystem IN ('benchmark','annotation_qa') THEN 'active_experiment'
           WHEN r.source LIKE 'outputs/tests/%' THEN 'historical_report'
           WHEN r.source LIKE 'outputs/books/%' THEN 'book'
           WHEN r.source LIKE 'data/annotations/%' THEN 'annotation'
           WHEN r.source LIKE 'data/engravings/%' THEN 'engraving'
           WHEN r.source LIKE 'data/indexes/%' THEN 'current_index'
           ELSE 'other_current_product' END AS category,
      count(*) AS occurrences,min(r.id) AS example_id
    FROM assets a JOIN census.refs r ON r.normalized=a.path
    LEFT JOIN source_states ss ON ss.source=r.source
    WHERE NOT (ss.state='superseded' AND EXISTS(SELECT 1 FROM record_files rf WHERE rf.path=a.path AND rf.metadata_path=ss.metadata_path))
    GROUP BY a.path,category;
    CREATE INDEX dependencies_target ON dependencies(target);
    CREATE TABLE record_dependencies AS SELECT rf.metadata_path,d.category,sum(d.occurrences) AS occurrences,min(d.example_id) AS example_id
      FROM record_files rf JOIN dependencies d ON d.target=rf.path GROUP BY rf.metadata_path,d.category;
    CREATE INDEX record_dependencies_metadata ON record_dependencies(metadata_path);
    CREATE TABLE replacement_summary AS SELECT rep.metadata_path,count(*) AS references_count,
      sum(CASE WHEN r.assignment='active' THEN 1 ELSE 0 END) AS active_replacements,
      sum(CASE WHEN r.metadata_path IS NULL THEN 1 ELSE 0 END) AS unresolved_replacements
      FROM replacements rep LEFT JOIN census.records r ON r.metadata_path=rep.target GROUP BY rep.metadata_path;
    CREATE UNIQUE INDEX replacement_metadata ON replacement_summary(metadata_path);
    """)
    database.commit()


def _export(project, database, output, summary):
    dependency_stream = iter(database.execute("SELECT metadata_path,category,occurrences,example_id FROM record_dependencies ORDER BY metadata_path,category"))
    dependency = next(dependency_stream, None)
    file_stream = iter(database.execute("SELECT rf.metadata_path,a.path,a.destination,a.size,a.sha256,a.reasons FROM record_files rf JOIN assets a ON a.path=rf.path ORDER BY rf.metadata_path,a.path"))
    asset = next(file_stream, None)
    rows = database.execute("SELECT c.*,coalesce(r.active_replacements,0),coalesce(r.unresolved_replacements,0) FROM chosen c LEFT JOIN replacement_summary r ON r.metadata_path=c.metadata_path ORDER BY c.metadata_path")
    with (output / "archive-plan.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            metadata_path, png_path, media, filename, shot, field, label, old_id, replacements, raw_reasons, metadata_hash, inputs, active_replacements, unresolved = row
            reasons = json.loads(raw_reasons)
            files = []
            dependencies = []
            historical = 0
            while asset is not None and asset[0] == metadata_path:
                _, path, destination, size, sha256, issues = asset
                reasons.extend(json.loads(issues))
                checksum_status = ("prior_authoritative_metadata_current_stat_checked" if summary.get("revalidation") else "freshly_verified_metadata") if path == metadata_path and metadata_hash == sha256 else "previous_census_verified_current_bytes_pending" if sha256 else "unavailable"
                if path == metadata_path and metadata_hash and sha256 != metadata_hash:
                    reasons.append("metadata_checksum_differs_from_census")
                files.append({"original_path": path, "archive_path": destination, "lifecycle_state": "superseded", "file_size": size, "checksum": sha256, "checksum_status": checksum_status})
                asset = next(file_stream, None)
            while dependency is not None and dependency[0] == metadata_path:
                _, category, occurrences, example_id = dependency
                dependencies.append({"category": category, "occurrences": occurrences, "example_id": example_id})
                if category.startswith("historical_"):
                    historical += occurrences
                else:
                    reasons.append("active_dependency:" + category)
                dependency = next(dependency_stream, None)
            if unresolved:
                reasons.append("unresolved_replacement_reference")
            reasons = sorted(set(reasons))
            eligible = eligibility(reasons, historical)
            summary["eligibility"][eligible] += 1
            summary["media"][media] += 1
            summary["no_active_replacement_records"] += not active_replacements
            for reason in reasons:
                summary["blocking_reasons"][reason] += 1
            if reasons and len(summary["blocking_examples"]) < 20:
                summary["blocking_examples"].append({"metadata_path": metadata_path, "reasons": reasons, "dependencies": dependencies})
            total_bytes = sum(item["file_size"] or 0 for item in files)
            record = {"source_metadata_path": metadata_path, "source_png_path": png_path, "proposed_archive_metadata_path": archive_path(metadata_path), "proposed_archive_png_path": archive_path(png_path) if png_path else None, "assignment_state": "superseded", "state_verification": ("retained_authoritative_json_current_stat_checked" if summary.get("revalidation") else "authoritative_json") if metadata_hash else "unverified_census_candidate", "media_type": media, "film_gameplay_source": filename, "shot_id": shot, "field": field, "label": label, "legacy_object_id": old_id, "original_record_identity": {"media_type": media, "filename_stem": filename, "label": label, "object_id": old_id}, "replacement_reference": json.loads(replacements), "active_replacements": active_replacements, "files": files, "associated_inputs_not_moved": json.loads(inputs), "total_bytes": total_bytes, "archive_eligibility": eligible, "blocking_reasons": reasons, "dependencies": dependencies}
            handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")
    with (output / "unique-file-plan.jsonl").open("w", encoding="utf-8") as handle:
        for path, destination, size, sha256, shared, reasons, owners, metadata in database.execute("SELECT a.path,a.destination,a.size,a.sha256,a.shared_active,a.reasons,count(*),min(rf.metadata_path) FROM assets a JOIN record_files rf ON rf.path=a.path GROUP BY a.path ORDER BY a.path"):
            handle.write(json.dumps({"original_path": path, "archive_path": destination, "lifecycle_state": "superseded", "file_size": size, "checksum": sha256, "checksum_status": "previous_census_verified_current_bytes_pending" if sha256 else "unavailable", "superseded_record_owners": owners, "representative_metadata_path": metadata, "identity_and_replacement_lookup": "archive-plan.jsonl", "shared_active": bool(shared), "issues": json.loads(reasons)}, separators=(",", ":")) + "\n")
    with (output / "dependency-examples.jsonl").open("w", encoding="utf-8") as handle:
        for target, category, count, source, pointer, value in database.execute("SELECT d.target,d.category,d.occurrences,r.source,r.pointer,r.value FROM dependencies d JOIN census.refs r ON r.id=d.example_id ORDER BY d.target,d.category"):
            handle.write(json.dumps({"target": target, "category": category, "occurrences": count, "representative_source": source, "pointer": pointer, "value": value}, separators=(",", ":")) + "\n")
    summary["selected_records"] = database.execute("SELECT count(*) FROM chosen").fetchone()[0]
    summary["unique_files"], summary["total_bytes"] = database.execute("SELECT count(*),coalesce(sum(size),0) FROM assets").fetchone()
    summary["average_file_bytes"] = summary["total_bytes"] / max(1, summary["unique_files"])
    summary["largest_files"] = [{"path": path, "bytes": size} for path, size in database.execute("SELECT path,size FROM assets ORDER BY size DESC,path LIMIT 10")]
    summary["dependency_occurrences"] = dict(database.execute("SELECT category,sum(occurrences) FROM dependencies GROUP BY category"))
    summary["many_predecessor_replacement_groups"] = database.execute("SELECT count(*) FROM(SELECT target FROM replacements GROUP BY target HAVING count(*)>1)").fetchone()[0]


def plan_archive(project_path: str | Path, *, version=None, revalidate=False, preparation=False) -> Path:
    if revalidate:
        if not version:
            raise ValueError("Revalidation requires an explicit new report version")
        return _revalidate_archive(Path(project_path).resolve(), version, preparation=preparation)
    if preparation:
        raise ValueError("Preparation reporting requires versioned revalidation")
    project = Path(project_path).resolve()
    output = _report_output(project, version)
    if any(path.is_symlink() for path in (project / "outputs", output.parent, output)) or not output.parent.is_dir():
        raise ValueError("Report parent must exist without symlinked ancestors")
    evidence = project / "outputs/tests/silhouette-identity-migration"
    baseline = json.loads((evidence / "safety-after.json").read_text())
    if not baseline.get("zero_protected_file_changes"):
        raise ValueError("A verified census baseline is required")
    if os.path.lexists(output / "plan.sqlite3") or (output.is_dir() and any(output.iterdir())):
        raise FileExistsError("Existing archive plan retained; choose human review rather than overwrite")
    output.mkdir(exist_ok=True)
    temp = output / "sqlite-temp"
    if temp.is_symlink():
        raise ValueError("Report temporary directory cannot be a symlink")
    temp.mkdir(exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = str(temp)
    database = sqlite3.connect(output / "plan.sqlite3", uri=True, timeout=300)
    database.execute("PRAGMA cache_size=-32768")
    database.execute("PRAGMA temp_store_directory='" + str(temp).replace("'", "''") + "'")
    database.execute("ATTACH DATABASE ? AS census", ((evidence / "census.sqlite3").as_uri() + "?mode=ro",))
    database.executescript("""
    CREATE TABLE chosen(metadata_path TEXT PRIMARY KEY,png_path TEXT,media TEXT,filename TEXT,shot TEXT,field TEXT,label TEXT,object_id TEXT,replacements TEXT,reasons TEXT,metadata_hash TEXT,inputs TEXT);
    CREATE TABLE record_files(metadata_path TEXT,path TEXT,PRIMARY KEY(metadata_path,path));
    CREATE TABLE assets(path TEXT PRIMARY KEY,size INTEGER,sha256 TEXT,mtime_ns INTEGER,kind TEXT,shared_active INTEGER DEFAULT 0,destination TEXT,reasons TEXT);
    CREATE TABLE source_states(source TEXT PRIMARY KEY,metadata_path TEXT,state TEXT);
    CREATE TABLE replacements(metadata_path TEXT,target TEXT,PRIMARY KEY(metadata_path,target));
    """)
    started = time.perf_counter()
    summary = {"dry_run": True, "execution_implemented": False, "census_candidates_no_longer_superseded": 0, "authoritative_superseded_confirmed": 0, "no_active_replacement_records": 0, "historical_label_not_exact_recheck_value": 0, "possible_atomic_fragment_of_compound_value": 0, "compound_examples": [], "blocking_examples": []}
    for key in ("causes", "recheck_results", "provenance_states", "provenance_methods", "eligibility", "media", "blocking_reasons"):
        summary[key] = Counter()
    lock = project / "data/silhouettes/.catalog-extraction.lock"
    lock_handle = lock.open("rb") if lock.is_file() else None
    try:
        if lock_handle:
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        before = _snapshot(project, database, output, "before")
        summary["census_population"] = dict(database.execute("SELECT assignment,count(*) FROM census.records GROUP BY assignment"))
        _read_candidates(project, database, summary)
        summary.update(_build_assets(project, database))
        _dependencies(database)
        _export(project, database, output, summary)
        summary["storage"] = _storage_state(project, summary)
        after = _snapshot(project, database, output, "after")
        comparison = compare_snapshots(output)
        summary["zero_detected_protected_changes"] = not before["changes_since_census"] and not after["changes_since_census"]
        summary["task_safety_comparison"] = comparison
        summary["namespace_integrity"] = verify_namespace_counts(project, database)
        summary["legacy_allocator_compatibility"] = allocator_reservations(database, output)
        summary["status"] = "BLOCKED — REQUIRES FIXES" if summary["blocking_reasons"] or not summary["zero_detected_protected_changes"] or summary["census_candidates_no_longer_superseded"] or summary["namespace_integrity"]["domain_count_differences"] or summary["legacy_allocator_compatibility"]["namespaces_with_historical_records"] else "READY FOR HUMAN REVIEW"
        summary["performance"] = {"seconds": time.perf_counter() - started, "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "pngs_decoded": 0, "checksums_reused": True}
        _artifact(output / "summary.json", summary)
        _write_report(output, summary)
    finally:
        database.close()
        if lock_handle:
            lock_handle.close()
    return output


def _report_output(project, version):
    root = project / "outputs/tests/silhouette-archive"
    if any(path.is_symlink() for path in (project / "outputs", root.parent, root)) or not root.parent.is_dir():
        raise ValueError("Report parent must exist without symlinked ancestors")
    if version is None:
        return root
    if not isinstance(version, str) or not re.fullmatch(r"v[0-9]+(?:-[a-z0-9-]+)?", version):
        raise ValueError("Version must be vNUMBER, optionally followed by a lowercase suffix")
    root.mkdir(exist_ok=True)
    output = root / version
    if os.path.lexists(output):
        raise FileExistsError("Versioned report already exists; retained evidence cannot be overwritten")
    return output


def _storage_state(project, summary):
    ancestor = project / "archive/silhouettes"
    while not ancestor.exists():
        ancestor = ancestor.parent
    info = os.statvfs(ancestor)
    free = info.f_bavail * info.f_frsize
    archive_device = ancestor.stat().st_dev
    same_filesystem = (project / "data/silhouettes").stat().st_dev == archive_device and all(device == archive_device for device in summary["source_filesystem_devices"])
    return {"archive_existing_ancestor": str(ancestor), "same_filesystem": same_filesystem, "atomic_individual_rename_possible": same_filesystem, "whole_operation_atomic": False, "available_bytes": free, "copy_capacity_sufficient": free >= max(summary["total_bytes"], summary["allocated_storage_bytes"]), "same_filesystem_rename_does_not_duplicate_content": same_filesystem, "archive_created": False}


def _revalidate_archive(project, version, *, preparation=False):
    root = project / "outputs/tests/silhouette-archive"
    output = _report_output(project, version)
    evidence = project / "outputs/tests/silhouette-identity-migration"
    summary = json.loads((root / "summary.json").read_text())
    if not summary.get("authoritative_superseded_confirmed") or summary.get("census_candidates_no_longer_superseded"):
        raise ValueError("Revalidation requires retained authoritative candidates")
    if any(path.is_symlink() for path in (root / "summary.json", root / "plan.sqlite3", evidence, evidence / "census.sqlite3")):
        raise ValueError("Symlinked retained evidence is not supported")
    output.mkdir()
    temp = output / "sqlite-temp"
    temp.mkdir()
    database = sqlite3.connect(output / "plan.sqlite3", uri=True, timeout=300)
    database.execute("PRAGMA cache_size=-32768")
    database.execute("PRAGMA temp_store_directory='" + str(temp).replace("'", "''") + "'")
    database.execute("ATTACH DATABASE ? AS census", ((evidence / "census.sqlite3").as_uri() + "?mode=ro",))
    database.execute("ATTACH DATABASE ? AS retained", ((root / "plan.sqlite3").as_uri() + "?mode=ro",))
    for table in ("chosen", "record_files", "source_states", "replacements"):
        database.execute(f"CREATE TEMP VIEW {table} AS SELECT * FROM retained.{table}")
    started = time.perf_counter()
    lock = project / "data/silhouettes/.catalog-extraction.lock"
    lock_handle = lock.open("rb") if lock.is_file() else None
    try:
        if lock_handle:
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        before = _snapshot(project, database, output, "before")
        relevant_drift = [item for item in before["changes_since_census"] if item["path"].startswith(("data/silhouettes/", "data/indexes/illustration/"))]
        if relevant_drift:
            raise ValueError("Catalog/index evidence drifted; retained candidates cannot be reused")
        phase = "confirmation-after" if (root / "safety-confirmation-after-files.jsonl").is_file() else "after"
        prior_comparison = compare_snapshots(output, phase, "before", baseline_output=root)
        database.execute("CREATE TABLE assets AS SELECT * FROM retained.assets")
        database.execute("CREATE UNIQUE INDEX assets_path ON assets(path)")
        summary["revalidation"] = True
        summary["evidence_reuse"] = {"candidate_json_rereads": 0, "candidate_source": "../plan.sqlite3", "historical_lookup": "plan.sqlite3", "condition": "All catalog/index size, mtime and kind still match census; index bytes freshly hashed. Selected metadata hashes retained, not freshly recomputed."}
        summary["no_active_replacement_records"] = 0
        summary["blocking_examples"] = []
        for key in ("eligibility", "media", "blocking_reasons"):
            summary[key] = Counter()
        summary.update(_validate_assets(project, database))
        _dependencies(database)
        _export(project, database, output, summary)
        summary["historical_access"] = {"explicit_only": True, "resolver": "services.silhouette_archive_reference.resolve_silhouette_reference", "lineage_consumer": "inspect_historical_lineage", "report_consumer": "resolve_historical_report_reference", "ordinary_archive_fallback": False, "preserves_original_records_and_reports": True}
        summary["storage"] = _storage_state(project, summary)
        after = _snapshot(project, database, output, "after")
        summary["task_safety_comparison"] = compare_snapshots(output)
        summary["prior_evidence_comparison"] = prior_comparison
        summary["preexisting_census_differences"] = before["changes_since_census"]
        summary["zero_detected_protected_changes"] = summary["task_safety_comparison"]["zero_detected_task_changes"]
        summary["namespace_integrity"] = verify_namespace_counts(project, database)
        summary["legacy_allocator_compatibility"] = allocator_reservations(database, output)
        summary["remaining_blockers"] = ["Approved production ordinal reservation initialization and verification", "Separately approved index transition for explicitly historical rows"]
        if summary["blocking_reasons"]:
            summary["remaining_blockers"].append("Unresolved runtime/shared/asset dependencies")
        if not prior_comparison["zero_detected_task_changes"] or prior_comparison["files_without_saved_current_metadata"]:
            summary["remaining_blockers"].append("Retained safety baseline differs or lacks current metadata")
        if not summary["zero_detected_protected_changes"] or summary["namespace_integrity"]["domain_count_differences"] or after["changes_since_census"] != before["changes_since_census"]:
            summary["remaining_blockers"].append("Protected file changes detected")
        summary["status"] = "BLOCKED — REQUIRES FIXES"
        if preparation:
            _preparation_summary(project, database, summary, evidence)
        summary["before_after"] = {"previous_eligibility": json.loads((root / "summary.json").read_text())["eligibility"], "current_eligibility": dict(summary["eligibility"]), "allocator_risk_not_deployed": 0 if summary["legacy_allocator_compatibility"]["production_deployment_verified"] else summary["legacy_allocator_compatibility"]["namespaces_at_risk"], "historical_allocator_risk_namespaces": summary["legacy_allocator_compatibility"]["namespaces_at_risk"]}
        summary["performance"] = {"seconds": time.perf_counter() - started, "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "pngs_decoded": 0, "checksums_reused": True}
        _artifact(output / "summary.json", summary)
        _write_report(output, summary)
    finally:
        database.close()
        if lock_handle:
            lock_handle.close()
    return output


def _preparation_summary(project, database, summary, evidence):
    from services.silhouette_legacy_allocation import registry_status

    registry = registry_status(project, evidence / "census.sqlite3")
    indexes = {}
    for media in ("movie", "gameplay"):
        path = project / f"data/indexes/illustration/{media}-silhouettes.sqlite3"
        if path.is_file():
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as index:
                indexes[media] = dict(index.execute("SELECT assignment_state,count(*) FROM records GROUP BY assignment_state"))
    categories = summary["dependency_occurrences"]
    asset_reasons = Counter()
    for raw, count in database.execute("SELECT reasons,count(*) FROM assets GROUP BY reasons"):
        for reason in json.loads(raw):
            asset_reasons[reason] += count
    unresolved = database.execute("SELECT coalesce(sum(unresolved_replacements),0) FROM replacement_summary").fetchone()[0]
    summary["stage1_preparation"] = {
        "registry": registry,
        "consumer_policy": "Ordinary catalog scans, QA, generation, evidence and Book insertion authorize active canonical records. Historical forensic/lifecycle scans and pinned benchmark references are explicit exceptions; provenance is unchanged.",
        "index_transition": {"current_rows": indexes, "schema_change_required": False, "production_rebuild_performed": False, "tested": True, "action_at_archival": "Under the catalog lock, move only the approved superseded assets using the future authorized executor, then rebuild movie and gameplay Illustration silhouette indexes. New rebuilds retain active rows only, preserve lineage payloads, and never scan archive. Existing old indexes continue active-default queries until replaced. Archive manifests provide historical lookup."},
        "risk_counts": {"active_only_consumer_violations": 0, "historical_references_safely_resolvable": categories.get("historical_lineage_resolvable", 0) + categories.get("historical_report", 0), "historical_references_unresolved": unresolved, "legacy_existing_ids_unreserved": registry.get("unreserved_existing_ids"), "index_transition_implementation_risks": 0, "active_runtime_dependency_records": sum(count for reason, count in summary["blocking_reasons"].items() if reason.startswith("active_dependency:")), "filesystem_collisions": asset_reasons.get("destination_collision", 0), "missing_files": asset_reasons.get("missing_or_unsafe_source", 0)},
        "intentional_exceptions": ["Catalog audit, canonical-search/number/provenance audits and lifecycle rechecks explicitly scan historical working records", "Explicitly pinned benchmark reference_cases are read-only baselines, not normal candidate selection", "Explicit include_history evidence/QA inspection and include_archive resolver", "Existing self-contained Book pixels and derived preprocessing renders are not catalog candidate selection"],
    }
    summary["legacy_allocator_compatibility"]["production_deployment_verified"] = registry["ready"]
    summary["legacy_allocator_compatibility"]["registry_path"] = registry["path"]
    summary["remaining_blockers"] = [reason for reason in summary["remaining_blockers"] if reason not in ("Approved production ordinal reservation initialization and verification", "Separately approved index transition for explicitly historical rows")]
    if not registry["ready"]:
        summary["remaining_blockers"].append("Production ordinal registry missing, invalid or incomplete")
    if unresolved:
        summary["remaining_blockers"].append("Historical replacement references unresolved")
    summary["required_execution_steps"] = ["Human approval of archive manifest and executor/journal/rollback design", "Production active-only Illustration silhouette index rebuild at the approved archival transition"]
    summary["status"] = "BLOCKED — REQUIRES FIXES" if summary["remaining_blockers"] else "READY FOR ARCHIVAL REVIEW"


def _write_report(output, summary):
    audit = [
        ("services/illustration_index.py::_scan_silhouettes / _default_assignment_state", "active-only", "Rebuild scans active working records only; normal queries default active even on old mixed indexes. Existing production historical rows remain until the separately approved rebuild at archival. Archive is never scanned."),
        ("visualizers/components/illustration_source.py::SilhouetteSource.records / page", "safe", "SQLite query default active; no filesystem scan. Explicit historical paths would change after archival."),
        ("visualizers/annotation_visualizer.py / visualizers/shot_visualizer.py", "safe", "Annotation UI is the Shotlist UI; no silhouette catalog consumer found in shot_visualizer."),
        ("services/annotation_qa_qwen_vl.py::prepare_cases", "active-only", "Reloads and authorizes catalog lifecycle before preparing images. Explicit include_history is an inspection-only exception. Active QA simulation tested; no model experiment executed."),
        ("services/engraving_batch.py::scan_best_silhouettes / batch_generate_engravings", "active-only", "Best selection filters canonical active state; generation reloads source records so stale supplied metadata cannot bypass the gate."),
        ("services/engraving_generate_openai.py / engraving_smoke.py / engraving_local_pipeline.py / engraving_generate.py", "active-only", "Catalog direct inputs authorize canonical state before generation. Legacy raw-PNG generation checks catalog sidecars. Self-contained copied Book/derived preprocessing inputs are not catalog candidate selection. No generation executed."),
        ("services/engraving_benchmark.py::_fetch_pool / _load_reference_records", "safe", "Read-only SQLite pool selection explicitly assignment_state=active; pinned/frozen reference paths are intentional exceptions and remain dependencies."),
        ("services/engraving_benchmark_evidence.py::shot_evidence_for_record", "active-only", "Checks canonical state and reloads record_path when supplied. include_history is explicit read-only evidence inspection."),
        ("services/silhouette_reference.py / services/silhouette_discovery.py", "safe", "Active-index candidate authorization and catalog-root validation; changing root location requires explicit historical resolution."),
        ("visualizers/book_visualizer.py::_insert_silhouette_at / data/book.py", "active-only silhouette insertion", "Book's named silhouette insertion method also serves its generated EngravingSource browser. Generated engraving copies remain unchanged; direct silhouette insertion reloads canonical active state before copying. Existing Book-local pixels are self-contained."),
        ("mcp_server/mcp_server.py::list_silhouettes / list_silhouette_candidates / get_best_silhouette / generate_silhouette_booklet", "safe", "Delegates to active-index silhouette_discovery; no second catalog scan."),
        ("cli.py::cmd_index (silhouette list/summary/rank/cluster)", "safe", "Canonical silhouette_discovery active queries; raw audits and rechecks are separate operations."),
        ("services/silhouette_catalog.py::iter_catalog / scan_catalog / audit_catalog", "active-only with explicit audit exception", "Default scanners exclude inactive, superseded and unreadable entries. Physical audit explicitly requests all working-tree states. Archive is never scanned."),
        ("services/silhouette_catalog.py::_next_object_index", "registry status in summary", "Uses installed central legacy high-water registry first, then transitional markers. Atomic batch reservation protects historical maxima and new namespaces. Initialization and deployed coverage are measured separately in Stage 1 summary."),
        ("services/silhouette_scoring.py / services/silhouette_semantics.py", "active-only", "Use canonical active-default scan_catalog; lifecycle maintenance and forensic callers explicitly request historical states."),
        ("services/silhouette_archive_reference.py::inspect_historical_lineage / resolve_historical_report_reference", "explicit historical access", "Central resolver reads original legacy paths or checksum-verified archive mappings only on include_archive=True. Immediate chain edges and report fields can be inspected without rewriting originals. Normal active discovery ignores archive."),
        ("services/silhouette_curation.py / services/silhouette_recheck.py", "intentionally historical", "Recheck writers retain film/label-scoped lineage identities, not runtime image dependencies. No visualizer/recheck reader follows active supersedes identities. Explicit inspection uses the central resolver; mutating rechecks are not given historical fallback."),
        ("services/silhouette_provenance.py / services/silhouette_number_audit.py / canonical-search and direction audit services", "intentionally historical", "Forensic full-catalog audit/migration inputs intentionally include historical states. Existing reports and frozen original paths must be preserved, not rewritten."),
    ]
    audit_lines = ["# Active Consumer Audit", "", "Ordinary discovery and consumption are active-only and archive-unaware. Explicit historical inspection and report-reference recovery use the central resolver; deployed registry coverage is reported, never inferred from fixture mechanics.", "", "| Source / Function | Classification | Selection, storage and path implications |", "| --- | --- | --- |"]
    audit_lines.extend(f"| {source} | {classification} | {detail} |" for source, classification, detail in audit)
    (output / "consumer-audit.md").write_text("\n".join(audit_lines) + "\n")
    sections = ["# Superseded Silhouette Archive Dry Run", "", "No archive was created; no production data or indexes were changed.", "", "## A. What Are Superseded Silhouettes?", "Explicit assignment.state=superseded means a completed retirement/recheck. Missing assignment remains active; inactive is excluded. This is not a visual-quality verdict.", "", "## B. Why Were They Superseded?", "Source-job rechecks rerun distinct current atomic annotation values and share replacement sets across predecessors; zero replacements is a valid completed retirement. Single-identity rechecks also exist. Canonical-search questionable provenance means the current source shot was not returned, not that the mask is wrong. Token fragments of current compound values are a diagnostic, not proof of the historical decomposition bug or causal attribution.", "", "## C. How Many Would Be Archived?", f"Records: {summary['selected_records']:,}; unique files: {summary['unique_files']:,}; bytes: {summary['total_bytes']:,}. Record file lists and unique physical-file lists are separate.", "", "## D. What Would Remain Active?", f"Unchanged census population: {dict(summary['census_population'])}. Active and inactive records are excluded. Shared active files and active references block movement; no moves were attempted.", "", "## E. What Depends on Historical Data?", "See consumer-audit.md and dependency-examples.jsonl. Active lifecycle backlinks, indexes, annotations, engravings, actual Book source links and frozen experiments are current dependencies. Self references are excluded; historical-to-historical links and read-only audit reports are retained historical dependencies, not erased. Self-contained Book copies are not assumed to depend on their original source.", "", "## F. What Blocks Archiving?", f"Eligibility: {dict(summary['eligibility'])}. Blocking reasons: {dict(summary['blocking_reasons'])}. Representatives and detailed counts are in summary.json and archive-plan.jsonl. Moving originals while active/indexed backlinks retain catalog identities requires an archive-aware resolver and a separately authorized index transition; neither is implemented.", "", "## G. Can the Archive Use Filesystem Renames?", json.dumps(summary['storage'], sort_keys=True), "Individual same-filesystem renames can be atomic; a million-file operation cannot. A future journal must make interruption and rollback explicit; collisions must never overwrite destinations.", "", "## H. Is the Operation Reversible?", "The manifest preserves original relative hierarchy, legacy identity, replacement references, sizes and census checksums. A future executor would journal each original/archive pair, verify content before movement and verify both sides before rollback. JSON and legacy object names remain unchanged. Existing absolute paths inside metadata will not repair themselves when moved; external identity resolution is required. Checksum reuse is not fresh PNG verification.", "", "## I. Is the Archive Ready for Execution?", summary['status'], "No apply flag or executor exists. Stop for human review.", "", "## Evidence", "```json", json.dumps(summary, indent=2, ensure_ascii=True), "```"]
    population = summary["census_population"]
    sections.insert(sections.index("## E. What Depends on Historical Data?"), f"After a future fully approved archival, the working tree would retain {population.get('active',0):,} active and {population.get('inactive',0):,} inactive records; active status would not change.")
    sections.insert(sections.index("## C. How Many Would Be Archived?"), f"Observed retirement mechanisms: {dict(summary['causes'])}; recheck outcomes: {dict(summary['recheck_results'])}; provenance methods: {dict(summary['provenance_methods'])}. Compound-fragment diagnostic matches: {summary['possible_atomic_fragment_of_compound_value']:,}; this does not prove the absence of historical segmentation bugs or identify their causal share.")
    if summary.get("legacy_allocator_compatibility"):
        sections.insert(sections.index("## G. Can the Archive Use Filesystem Renames?"), "Legacy allocator compatibility: " + json.dumps(summary["legacy_allocator_compatibility"], sort_keys=True))
    sections.extend(["", "## Safety Interpretation", "Pre-existing differences from an older census are not changes caused by this dry run. safety-before/after files distinguish fresh critical-product hashes from metadata-only comparisons. The task comparison is reported separately. No missing baseline files were silently accepted; a stale census blocks readiness."])
    sections.extend(["", "## Compatibility Revalidation", "Only identity-reconstructible assignment.supersedes edges are classified as historical lineage. Literal paths, other active references, shared assets, missing mappings and asset issues remain blockers. Explicit include_archive plus an assets lookup preserves original JSON and old CSV/JSONL fields; fallback never participates in active discovery.", "Production ordinal reservation initialization is NOT performed. Fixture-tested mechanics do not establish deployment readiness. A separate approved index transition is still required for historical index rows."])
    if summary.get("revalidation"):
        sections.extend(["Retained authoritative candidate JSON/checksums are reused only after current catalog/index metadata checks. PNG bytes and selected JSON bytes were not freshly rehashed in this rerun. Critical products are freshly hashed. Pre-existing census differences are separate from current before/after changes.", "Before/after: " + json.dumps(summary["before_after"], sort_keys=True), "Remaining blockers: " + json.dumps(summary["remaining_blockers"]), "Recommendation: " + summary["status"]])
    if summary.get("stage1_preparation"):
        sections = [text.replace("No archive was created; no production data or indexes were changed.", "No archive was created; no existing silhouette records, images or production indexes were changed. The separate authorized reservation-registry publication is recorded in Stage 1 evidence.").replace("Production ordinal reservation initialization is NOT performed. Fixture-tested mechanics do not establish deployment readiness. A separate approved index transition is still required for historical index rows.", "This planner does not initialize reservations. Installed registry readiness is measured below; the tested active-only index rebuild remains a deferred execution step, not a production rebuild in this task.") for text in sections]
        sections.extend(["", "## Stage 1 Preparation", json.dumps(summary["stage1_preparation"], sort_keys=True, indent=2), "Production indexes were not rebuilt and silhouette files were not moved. The validated registry is the sole authorized production-data addition; earlier generic deployment caveats are superseded by this measured Stage 1 section."])
    (output / "report.md").write_text("\n".join(sections) + "\n")