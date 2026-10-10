"""Streaming read-only archive census. Writes only isolated research reports."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import resource
import sqlite3
import stat
import time
import unicodedata
from collections import Counter
from pathlib import Path

from services.silhouette_identity import SLUG_VERSION, canonical_shot, label_slug, normalize_reference, plan_empty_catalog_parents, proposed_directory, tentative_instance_id


def artifact(path: Path, data) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=True, indent=2)
        handle.write("\n")


def walk_files(root: Path, excluded: Path):
    stack = [root]
    while stack:
        directory = stack.pop()
        if directory == excluded:
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                else:
                    yield path


def protected_files(project: Path, output: Path):
    for name in ("data", "outputs", "preferences"):
        root = project / name
        if root.is_dir():
            yield from walk_files(root, output)


def fingerprint(path: Path, retain_document: bool = True) -> tuple[dict, bytes | None]:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode):
        return {"sha256": hashlib.sha256(os.readlink(path).encode()).hexdigest(), "size": before.st_size, "mtime_ns": before.st_mtime_ns, "kind": "symlink"}, None
    if not stat.S_ISREG(before.st_mode):
        return {"sha256": None, "size": before.st_size, "mtime_ns": before.st_mtime_ns, "kind": "special"}, None
    digest = hashlib.sha256()
    data = None
    with path.open("rb") as handle:
        if retain_document and path.suffix.lower() == ".json":
            data = handle.read()
            digest.update(data)
        else:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    after = path.lstat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError(f"File changed while reading: {path}")
    return {"sha256": digest.hexdigest(), "size": before.st_size, "mtime_ns": before.st_mtime_ns, "kind": "regular"}, data


def create_tables(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE files(path TEXT PRIMARY KEY, domain TEXT, size INTEGER, mtime_ns INTEGER, sha256 TEXT, kind TEXT, seen INTEGER DEFAULT 0);
    CREATE TABLE records(metadata_path TEXT PRIMARY KEY, png_path TEXT, media_type TEXT, media_id TEXT, shot_id TEXT, field TEXT, label TEXT, slug TEXT, object_id TEXT, instance_id TEXT, destination TEXT, assignment TEXT, filename TEXT, frame INTEGER, visual_key TEXT, status TEXT, flags TEXT);
    CREATE TABLE labels(label TEXT PRIMARY KEY, slug TEXT, lowercase TEXT, whitespace TEXT, separators TEXT, nonascii INTEGER, punctuation INTEGER);
    CREATE TABLE refs(id INTEGER PRIMARY KEY, source TEXT, pointer TEXT, value TEXT, normalized TEXT, kind TEXT, subsystem TEXT);
    CREATE TABLE indexed(metadata_path TEXT, index_path TEXT, row_identity TEXT, assignment TEXT, old_id TEXT);
    CREATE TABLE problems(id INTEGER PRIMARY KEY, path TEXT, category TEXT, detail TEXT);
    CREATE TABLE shots(shot_id TEXT, filename TEXT, start INTEGER, end INTEGER);
    CREATE TABLE audited_csv(path TEXT PRIMARY KEY, rows INTEGER);
    """)


def interpret_record(relative: str, metadata: dict) -> dict:
    path = Path(relative)
    pieces = path.parts
    known_structure = len(pieces) == 7 and pieces[:3] == ("data", "silhouettes", "catalog")
    media_type = str(metadata.get("media_type") or (pieces[3] if known_structure else ""))
    field = str(metadata.get("field") or "")
    label = metadata.get("label")
    label = label if isinstance(label, str) else ""
    shot = str(metadata.get("shot_id") or "")
    media_id = str(metadata.get("media_id") or "")
    object_id = path.stem
    flags = ["old_ordinal_is_film_label_scoped_not_registered_shot_instance"]
    status = "needs-review"
    instance = None
    destination = None
    png_name = metadata.get("png")
    if not isinstance(png_name, str) or not png_name:
        png_name = object_id + ".png"
        flags.append("missing_png_name_in_metadata")
    if Path(png_name).name != png_name or "\\" in png_name:
        png_path = None
        flags.append("unsafe_or_nonlocal_png_reference")
        status = "ambiguous"
    else:
        png_path = (path.parent / png_name).as_posix()
    if not known_structure:
        flags.append("unexpected_directory_structure")
        status = "legacy"
    if metadata.get("object_id") not in (None, object_id):
        flags.append("explicit_object_id_disagrees_with_filename")
        status = "ambiguous"
    try:
        if not media_id:
            raise ValueError("Missing explicit metadata media_id")
        if not label or not field:
            raise ValueError("Missing label or field")
        instance = tentative_instance_id(shot, object_id, media_id)
        destination = proposed_directory(media_type, field, label, instance)
        from data.media_id import parse_shot_id
        _, start, end = parse_shot_id(shot)
        frame = metadata.get("frame")
        if not isinstance(frame, int) or not start <= frame <= end:
            flags.append("source_frame_missing_or_outside_shot")
            status = "ambiguous"
    except (ValueError, TypeError) as exc:
        flags.append(str(exc))
        status = "missing-identity"
    assignment = metadata.get("assignment", {})
    assignment = assignment.get("state", "active") if isinstance(assignment, dict) else "unknown"
    if assignment not in {"active", "inactive", "superseded"}:
        flags.append("unknown_assignment_state")
    visual_key = json.dumps([shot, metadata.get("frame"), metadata.get("bbox"), metadata.get("mask_area")], separators=(",", ":"))
    return {"metadata_path": relative, "png_path": png_path, "media_type": media_type, "media_id": media_id, "shot_id": shot, "field": field, "label": label, "slug": label_slug(label), "object_id": object_id, "instance_id": instance, "destination": destination, "assignment": assignment, "filename": str(metadata.get("filename_stem") or path.parent.parent.name), "frame": metadata.get("frame") if isinstance(metadata.get("frame"), int) else None, "visual_key": visual_key, "status": status, "flags": json.dumps(flags)}


def subsystem_for(relative: str) -> str:
    if relative.startswith("outputs/books/"):
        return "books"
    if relative.startswith("outputs/tests/engraving-benchmark/"):
        return "benchmark"
    if relative.startswith("outputs/tests/annotation-qa-qwen-vl/"):
        return "annotation_qa"
    return "/".join(Path(relative).parts[:2])


def collect_references(connection: sqlite3.Connection, project: Path, source: str, data, subsystem: str, pointer: str = "", context: Path | None = None) -> None:
    if isinstance(data, dict):
        for key in ("path", "record_path", "source_json"):
            value = data.get(key)
            if isinstance(value, str):
                normalized = normalize_reference(project, value)
                if normalized:
                    context = project / Path(normalized).parent
                    break
        if all(data.get(key) for key in ("media_type", "filename_stem", "label", "object_id")):
            from services.silhouette_catalog import _safe_label
            normalized = f"data/silhouettes/catalog/{data['media_type']}/{data['filename_stem']}/{_safe_label(str(data['label']))}/{data['object_id']}.json"
            connection.execute("INSERT INTO refs(source,pointer,value,normalized,kind,subsystem) VALUES(?,?,?,?,?,?)", (source, pointer, json.dumps({key: data[key] for key in ("media_type", "filename_stem", "label", "object_id")}), normalized, "identity_reconstructible", subsystem))
        for key, value in data.items():
            collect_references(connection, project, source, value, subsystem, pointer + "/" + str(key).replace("~", "~0").replace("/", "~1"), context)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            collect_references(connection, project, source, value, subsystem, pointer + "/" + str(index), context)
    elif isinstance(data, str):
        basename = bool(re.fullmatch(r"object_\d+\.(png|json)", data))
        normalized = normalize_reference(project, data, context if basename else None)
        if normalized or basename:
            kind = "filename_only" if basename else "absolute" if data.startswith("/") or re.match(r"^[A-Za-z]:", data) else "relative"
            connection.execute("INSERT INTO refs(source,pointer,value,normalized,kind,subsystem) VALUES(?,?,?,?,?,?)", (source, pointer, data, normalized, kind, subsystem))


def collect_csv_references(connection: sqlite3.Connection, project: Path, path: Path, *, collect_shots: bool = False) -> int:
    relative = path.relative_to(project).as_posix()
    count = 0
    csv.field_size_limit(64 * 1024 * 1024)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for count, row in enumerate(csv.DictReader(handle), 1):
            decoded = {}
            for key, value in row.items():
                if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                decoded[key] = value
            collect_references(connection, project, f"{relative}#row[{count}]", decoded, subsystem_for(relative))
            if collect_shots and relative.startswith("data/shotlists/") and row.get("shot_id"):
                connection.execute("INSERT INTO shots VALUES(?,?,?,?)", (row["shot_id"], path.stem, int(row["start_frame"]), int(row["end_frame"])))
            if count % 100000 == 0:
                print(f"CSV reference audit: {relative}, {count:,} rows processed", flush=True)
    return count


def scan_before(project: Path, output: Path, connection: sqlite3.Connection, performance: dict) -> None:
    started = time.perf_counter()
    count = 0
    byte_count = 0
    for path in protected_files(project, output):
        relative = path.relative_to(project).as_posix()
        info, raw = fingerprint(path)
        domain = "/".join(Path(relative).parts[:2])
        connection.execute("INSERT INTO files(path,domain,size,mtime_ns,sha256,kind) VALUES(?,?,?,?,?,?)", (relative, domain, info["size"], info["mtime_ns"], info["sha256"], info["kind"]))
        count += 1
        byte_count += info["size"]
        if raw is not None and path.suffix.lower() == ".json":
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                connection.execute("INSERT INTO problems(path,category,detail) VALUES(?,?,?)", (relative, "malformed-json", str(exc)))
                data = None
            if relative.startswith("data/silhouettes/"):
                if isinstance(data, dict) and ("shot_id" in data or re.fullmatch(r"object_\d+", path.stem)):
                    record = interpret_record(relative, data)
                    connection.execute("INSERT INTO records VALUES(" + ",".join("?" for _ in record) + ")", tuple(record.values()))
                    label = record["label"]
                    connection.execute("INSERT OR IGNORE INTO labels VALUES(?,?,?,?,?,?,?)", (label, label_slug(label), label.lower(), " ".join(label.split()), re.sub(r"[\s_-]+", " ", label.lower()).strip(), int(not label.isascii()), int(any(unicodedata.category(char)[0] in {"P", "S"} for char in label))))
                elif data is not None:
                    connection.execute("INSERT INTO problems(path,category,detail) VALUES(?,?,?)", (relative, "legacy-json", "Not a flat catalog object record"))
            if data is not None and not domain.startswith("preferences/keys"):
                context = path.parent if relative.startswith("data/silhouettes/") else None
                collect_references(connection, project, relative, data, subsystem_for(relative), context=context)
        elif path.suffix.lower() == ".jsonl" and info["kind"] == "regular":
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    try:
                        data = json.loads(line)
                    except (ValueError, UnicodeError) as exc:
                        connection.execute("INSERT INTO problems(path,category,detail) VALUES(?,?,?)", (relative, "malformed-jsonl-line", f"{line_number}: {exc}"))
                        continue
                    collect_references(connection, project, f"{relative}#line[{line_number}]", data, subsystem_for(relative))
        elif info["kind"] == "regular" and path.suffix.lower() == ".csv":
            try:
                csv_rows = collect_csv_references(connection, project, path, collect_shots=True)
                connection.execute("INSERT INTO audited_csv VALUES(?,?)", (relative, csv_rows))
            except (ValueError, KeyError, UnicodeError, csv.Error) as exc:
                connection.execute("INSERT INTO problems(path,category,detail) VALUES(?,?,?)", (relative, "csv-parse", str(exc)))
        if count % 25000 == 0:
            connection.commit()
            print(f"Protected snapshot/census: {count:,} files, {byte_count / 2**30:.1f} GiB, {time.perf_counter() - started:.1f}s", flush=True)
    connection.commit()
    performance.update({"before_seconds": time.perf_counter() - started, "protected_files": count, "protected_bytes": byte_count, "fingerprint_stat_calls_before": count * 2, "hash_file_reads_before": count})
    artifact(output / "safety-before.json", {"complete": True, "files": count, "bytes": byte_count, "per_file_sha256_and_mtime": "census.sqlite3:files", "scope": ["data/**", "outputs/** except this isolated report directory", "preferences/**"], "digest": inventory_digest(connection), "performance": performance})


def inventory_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for row in connection.execute("SELECT path,size,mtime_ns,sha256,kind FROM files ORDER BY path"):
        digest.update(json.dumps(row, separators=(",", ":")).encode())
    return digest.hexdigest()


def scan_indexes(project: Path, connection: sqlite3.Connection, performance: dict) -> None:
    started = time.perf_counter()
    indexes = []
    for (relative,) in connection.execute("SELECT path FROM files WHERE domain='data/indexes' AND path LIKE '%.sqlite3'").fetchall():
        source = sqlite3.connect((project / relative).as_uri() + "?mode=ro", uri=True)
        try:
            for (table,) in source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [row[1] for row in source.execute(f"PRAGMA table_info({quoted})")]
                if "payload" in columns:
                    count = 0
                    for row_id, payload in source.execute(f"SELECT rowid,payload FROM {quoted}"):
                        count += 1
                        try:
                            record = json.loads(payload)
                        except (ValueError, TypeError):
                            connection.execute("INSERT INTO problems(path,category,detail) VALUES(?,?,?)", (relative, "malformed-index-payload", str(row_id)))
                            continue
                        if relative.endswith("-silhouettes.sqlite3") and table == "records":
                            raw_path = record.get("path") or record.get("record_path")
                            normalized = normalize_reference(project, raw_path) if isinstance(raw_path, str) else None
                            assignment = record.get("assignment", {})
                            state = assignment.get("state", "active") if isinstance(assignment, dict) else "unknown"
                            connection.execute("INSERT INTO indexed VALUES(?,?,?,?,?)", (normalized, relative, str(row_id), state, record.get("object_id")))
                        collect_references(connection, project, f"{relative}#{table}[{row_id}]", record, "indexes", context=None)
                        if count % 25000 == 0:
                            connection.commit()
                    indexes.append({"path": relative, "table": table, "rows": count})
                else:
                    relevant = [column for column in columns if any(token in column.lower() for token in ("path", "json", "source", "metrics"))]
                    if relevant:
                        selected = ",".join('"' + column.replace('"', '""') + '"' for column in relevant)
                        for row_id, *values in source.execute(f"SELECT rowid,{selected} FROM {quoted}"):
                            for column, value in zip(relevant, values):
                                if isinstance(value, str):
                                    try:
                                        decoded = json.loads(value)
                                    except ValueError:
                                        decoded = value
                                    collect_references(connection, project, f"{relative}#{table}[{row_id}]", decoded, "indexes", pointer="/" + column)
        finally:
            source.close()
    connection.commit()
    performance["index_scan_seconds"] = time.perf_counter() - started
    performance["index_queries"] = indexes


def analyze(connection: sqlite3.Connection, project: Path, performance: dict) -> dict:
    started = time.perf_counter()
    connection.executescript("""
    CREATE INDEX record_instance ON records(instance_id);
    CREATE INDEX record_destination ON records(destination);
    CREATE INDEX record_png ON records(png_path);
    CREATE INDEX record_visual ON records(visual_key);
    CREATE INDEX indexed_path ON indexed(metadata_path);
    CREATE INDEX ref_target ON refs(normalized);
    CREATE INDEX shots_id ON shots(shot_id);
    CREATE TABLE identity_collisions AS SELECT instance_id,count(*) AS members FROM records WHERE instance_id IS NOT NULL GROUP BY instance_id HAVING count(*)>1;
    CREATE UNIQUE INDEX collision_instance ON identity_collisions(instance_id);
    CREATE TABLE destination_collisions AS SELECT destination,count(*) AS members FROM records WHERE destination IS NOT NULL GROUP BY destination HAVING count(*)>1;
    CREATE UNIQUE INDEX collision_destination ON destination_collisions(destination);
    CREATE TABLE slug_collisions AS SELECT slug,count(*) AS labels FROM labels GROUP BY slug HAVING count(*)>1 OR slug='';
    CREATE UNIQUE INDEX collision_slug ON slug_collisions(slug);
    CREATE TABLE visual_ambiguities AS SELECT visual_key,count(*) AS members FROM records GROUP BY visual_key HAVING count(*)>1;
    UPDATE records SET status='orphan' WHERE png_path IS NULL OR NOT EXISTS(SELECT 1 FROM files WHERE files.path=records.png_path);
    UPDATE records SET status='orphan' WHERE NOT EXISTS(SELECT 1 FROM indexed WHERE indexed.metadata_path=records.metadata_path);
    UPDATE records SET status='ambiguous' WHERE status='needs-review' AND slug IN(SELECT slug FROM slug_collisions);
    UPDATE records SET status='collision' WHERE instance_id IN(SELECT instance_id FROM identity_collisions) OR destination IN(SELECT destination FROM destination_collisions);
    """)
    connection.commit()
    def scalar(query):
        return connection.execute(query).fetchone()[0]
    def grouped(query):
        return dict(connection.execute(query).fetchall())
    summary = {
        "physical_silhouette_files": scalar("SELECT count(*) FROM files WHERE domain='data/silhouettes'"),
        "silhouette_pngs": scalar("SELECT count(*) FROM files WHERE domain='data/silhouettes' AND lower(path) LIKE '%.png'"),
        "silhouette_jsons": scalar("SELECT count(*) FROM files WHERE domain='data/silhouettes' AND lower(path) LIKE '%.json'"),
        "records": scalar("SELECT count(*) FROM records"),
        "malformed_silhouette_metadata": scalar("SELECT count(*) FROM problems WHERE category='malformed-json' AND path LIKE 'data/silhouettes/%'"),
        "missing_identity_records": scalar("SELECT count(*) FROM records WHERE instance_id IS NULL OR destination IS NULL"),
        "intrinsically_ambiguous_records": scalar("SELECT count(*) FROM records WHERE flags LIKE '%source_frame_missing_or_outside_shot%' OR flags LIKE '%explicit_object_id_disagrees%' OR flags LIKE '%unsafe_or_nonlocal%'"),
        "unexpected_record_structures": scalar("SELECT count(*) FROM records WHERE flags LIKE '%unexpected_directory_structure%'"),
        "assignment": grouped("SELECT assignment,count(*) FROM records GROUP BY assignment"),
        "media": grouped("SELECT media_type,count(*) FROM records GROUP BY media_type"),
        "fields": grouped("SELECT field,count(*) FROM records GROUP BY field"),
        "sources": scalar("SELECT count(*) FROM(SELECT DISTINCT media_type,filename FROM records)"),
        "labels": scalar("SELECT count(*) FROM labels"),
        "unique_slugs": scalar("SELECT count(DISTINCT slug) FROM labels"),
        "slug_collision_groups": scalar("SELECT count(*) FROM slug_collisions"),
        "slug_collision_labels": scalar("SELECT coalesce(sum(labels),0) FROM slug_collisions"),
        "empty_slugs": scalar("SELECT count(*) FROM labels WHERE slug=''"),
        "nonascii_labels": scalar("SELECT count(*) FROM labels WHERE nonascii=1"),
        "punctuation_labels": scalar("SELECT count(*) FROM labels WHERE punctuation=1"),
        "case_collision_groups": scalar("SELECT count(*) FROM(SELECT lowercase FROM labels GROUP BY lowercase HAVING count(*)>1)"),
        "whitespace_collision_groups": scalar("SELECT count(*) FROM(SELECT whitespace FROM labels GROUP BY whitespace HAVING count(*)>1)"),
        "separator_collision_groups": scalar("SELECT count(*) FROM(SELECT separators FROM labels GROUP BY separators HAVING count(*)>1)"),
        "identity_collision_groups": scalar("SELECT count(*) FROM identity_collisions"),
        "identity_collision_records": scalar("SELECT coalesce(sum(members),0) FROM identity_collisions"),
        "destination_collision_groups": scalar("SELECT count(*) FROM destination_collisions"),
        "destination_collision_records": scalar("SELECT coalesce(sum(members),0) FROM destination_collisions"),
        "missing_png_records": scalar("SELECT count(*) FROM records WHERE png_path IS NULL OR NOT EXISTS(SELECT 1 FROM files WHERE path=records.png_path)"),
        "pngs_without_records": scalar("SELECT count(*) FROM files WHERE domain='data/silhouettes' AND lower(path) LIKE '%.png' AND NOT EXISTS(SELECT 1 FROM records WHERE png_path=files.path)"),
        "pngs_without_sibling_json": scalar("SELECT count(*) FROM files AS png WHERE domain='data/silhouettes' AND lower(path) LIKE '%.png' AND NOT EXISTS(SELECT 1 FROM files AS metadata WHERE metadata.path=substr(png.path,1,length(png.path)-4) || '.json')"),
        "records_not_indexed": scalar("SELECT count(*) FROM records WHERE NOT EXISTS(SELECT 1 FROM indexed WHERE indexed.metadata_path=records.metadata_path)"),
        "index_rows_without_records": scalar("SELECT count(*) FROM indexed WHERE metadata_path IS NULL OR NOT EXISTS(SELECT 1 FROM records WHERE records.metadata_path=indexed.metadata_path)"),
        "duplicate_index_paths": scalar("SELECT count(*) FROM(SELECT metadata_path FROM indexed GROUP BY metadata_path HAVING count(*)>1)"),
        "duplicate_png_paths": scalar("SELECT count(*) FROM(SELECT png_path FROM records WHERE png_path IS NOT NULL GROUP BY png_path HAVING count(*)>1)"),
        "duplicate_legacy_identity_groups": scalar("SELECT count(*) FROM(SELECT media_type,filename,label,object_id FROM records GROUP BY 1,2,3,4 HAVING count(*)>1)"),
        "multiple_same_label_shot_groups": scalar("SELECT count(*) FROM(SELECT shot_id,field,label FROM records GROUP BY 1,2,3 HAVING count(*)>1)"),
        "multi_shot_old_namespaces": scalar("SELECT count(*) FROM(SELECT media_type,filename,label FROM records GROUP BY 1,2,3 HAVING count(DISTINCT shot_id)>1)"),
        "frame_bbox_repeat_groups": scalar("SELECT count(*) FROM(SELECT visual_key FROM records GROUP BY visual_key HAVING count(*)>1)"),
        "missing_current_shotlist_records": scalar("SELECT count(*) FROM records WHERE NOT EXISTS(SELECT 1 FROM shots WHERE shots.shot_id=records.shot_id)"),
        "object_name_lengths": grouped("SELECT length(object_id),count(*) FROM records GROUP BY 1"),
        "statuses": grouped("SELECT status,count(*) FROM records GROUP BY status"),
        "problem_categories": grouped("SELECT category,count(*) FROM problems GROUP BY category"),
        "references": scalar("SELECT count(*) FROM refs"),
        "csv_files_audited": scalar("SELECT count(*) FROM audited_csv"),
        "csv_rows_audited": scalar("SELECT coalesce(sum(rows),0) FROM audited_csv"),
        "reference_kinds": grouped("SELECT kind,count(*) FROM refs GROUP BY kind"),
        "reference_subsystems": grouped("SELECT subsystem,count(*) FROM refs GROUP BY subsystem"),
        "unresolved_filename_references": scalar("SELECT count(*) FROM refs WHERE normalized IS NULL"),
        "references_to_absent_files": scalar("SELECT count(*) FROM refs WHERE normalized IS NOT NULL AND NOT EXISTS(SELECT 1 FROM files WHERE files.path=refs.normalized)"),
        "indexed_assignment": grouped("SELECT assignment,count(*) FROM indexed GROUP BY assignment"),
        "ready": 0,
        "identity_policy_approved": False,
    }
    prefix_length = len(os.fsencode(str(project))) + 1
    connection.execute("CREATE TABLE lengths AS SELECT metadata_path,destination,length(CAST(destination || '/silhouette.json' AS BLOB)) + ? AS bytes FROM records WHERE destination IS NOT NULL", (prefix_length,))
    connection.execute("CREATE INDEX lengths_value ON lengths(bytes)")
    total = scalar("SELECT count(*) FROM lengths")
    if total:
        offsets = [0, (total - 1) // 2, total // 2, max(0, (95 * total + 99) // 100 - 1), total - 1]
        values = [connection.execute("SELECT bytes FROM lengths ORDER BY bytes LIMIT 1 OFFSET ?", (offset,)).fetchone()[0] for offset in offsets]
        summary["path_lengths_bytes"] = {"count": total, "min": values[0], "median": (values[1] + values[2]) / 2, "p95": values[3], "max": values[4], "component_over_255_records": scalar("SELECT count(*) FROM records WHERE length(CAST(slug AS BLOB))>255 OR length(CAST(instance_id AS BLOB))>255 OR length(CAST(field AS BLOB))>255"), "full_path_over_4096_records": scalar("SELECT count(*) FROM lengths WHERE bytes>=4096")}
        summary["longest_paths"] = [dict(zip(("old_metadata_path", "new_directory", "full_json_path_bytes"), row)) for row in connection.execute("SELECT metadata_path,destination,bytes FROM lengths ORDER BY bytes DESC,metadata_path LIMIT 10")]
    absent_targets = connection.execute("SELECT normalized,count(*) FROM refs WHERE normalized IS NOT NULL AND NOT EXISTS(SELECT 1 FROM files WHERE files.path=refs.normalized) GROUP BY normalized").fetchall()
    summary["directory_reference_occurrences"] = sum(count for relative, count in absent_targets if (project / relative).is_dir())
    summary["references_to_absent_files"] = sum(count for relative, count in absent_targets if not (project / relative).exists())
    summary["source_populations"] = [dict(zip(("media_type", "filename", "records"), row)) for row in connection.execute("SELECT media_type,filename,count(*) FROM records GROUP BY 1,2 ORDER BY 1,2")]
    summary["samples"] = []
    for media, field, state in connection.execute("SELECT DISTINCT media_type,field,assignment FROM records ORDER BY 1,2,3").fetchall():
        rows = connection.execute("SELECT metadata_path,shot_id,label,object_id,frame,instance_id FROM records WHERE media_type=? AND field=? AND assignment=? ORDER BY metadata_path LIMIT 2", (media, field, state)).fetchall()
        summary["samples"].extend({"media_type": media, "field": field, "assignment": state, **dict(zip(("metadata_path", "shot_id", "label", "object_id", "frame", "tentative_instance_id"), row))} for row in rows)
    summary["collision_examples"] = [dict(zip(("instance_id", "members"), row)) for row in connection.execute("SELECT instance_id,members FROM identity_collisions ORDER BY members DESC,instance_id LIMIT 10")]
    summary["collision_example_members"] = []
    for example in summary["collision_examples"]:
        summary["collision_example_members"].append({"instance_id": example["instance_id"], "records": [dict(zip(("old_metadata_path", "field", "label", "assignment"), row)) for row in connection.execute("SELECT metadata_path,field,label,assignment FROM records WHERE instance_id=? ORDER BY metadata_path", (example["instance_id"],))]})
    summary["old_silhouette_full_path_max_bytes"] = connection.execute("SELECT coalesce(max(length(CAST(path AS BLOB))),0) + ? FROM files WHERE domain='data/silhouettes'", (prefix_length,)).fetchone()[0]
    performance["analysis_seconds"] = time.perf_counter() - started
    connection.commit()
    return summary


def export_reports(project: Path, output: Path, connection: sqlite3.Connection, summary: dict, performance: dict) -> None:
    started = time.perf_counter()
    connection.row_factory = sqlite3.Row
    with (output / "migration-plan.json").open("w", encoding="utf-8") as handle:
        handle.write('{"read_only":true,"apply_implemented":false,"identity_policy_approved":false,"ordinal_mapping":"tentative legacy suffix, NOT registered shot instance","records":[')
        separator = ""
        for record in connection.execute("SELECT * FROM records ORDER BY metadata_path"):
            refs = connection.execute("SELECT id FROM refs WHERE normalized IN (?,?) ORDER BY id", (record["metadata_path"], record["png_path"])).fetchall()
            flags = json.loads(record["flags"])
            if connection.execute("SELECT 1 FROM identity_collisions WHERE instance_id=?", (record["instance_id"],)).fetchone():
                flags.append("proposed_global_instance_identity_collision")
            if connection.execute("SELECT 1 FROM destination_collisions WHERE destination=?", (record["destination"],)).fetchone():
                flags.append("proposed_destination_path_collision")
            if connection.execute("SELECT 1 FROM slug_collisions WHERE slug=?", (record["slug"],)).fetchone():
                flags.append("distinct_labels_collapse_to_same_slug")
            if not connection.execute("SELECT 1 FROM shots WHERE shot_id=? LIMIT 1", (record["shot_id"],)).fetchone():
                flags.append("shot_not_in_current_shotlists")
            mapping = {
                "old_path": record["png_path"], "old_metadata_path": record["metadata_path"],
                "media_type": record["media_type"], "media_id": record["media_id"], "shot_id": record["shot_id"],
                "field": record["field"], "original_label": record["label"], "label_slug": record["slug"],
                "old_object_id": record["object_id"], "proposed_instance_id": record["instance_id"],
                "proposed_new_directory": record["destination"],
                "proposed_new_files": {name: record["destination"] + "/" + name if record["destination"] else None for name in ("silhouette.png", "silhouette.json")},
                "references_found": {"count": len(refs), "ids": [row[0] for row in refs], "lookup": "references.jsonl"},
                "assignment_state": record["assignment"], "status": record["status"], "issues": flags,
            }
            handle.write(separator)
            json.dump(mapping, handle, separators=(",", ":"), ensure_ascii=True)
            separator = ",\n"
        handle.write("]}\n")
    with (output / "references.jsonl").open("w", encoding="utf-8") as handle:
        for row in connection.execute("SELECT * FROM refs ORDER BY id"):
            json.dump(dict(row), handle, separators=(",", ":"), ensure_ascii=True)
            handle.write("\n")
    with (output / "problematic-cases.jsonl").open("w", encoding="utf-8") as handle:
        for row in connection.execute("SELECT metadata_path,instance_id,destination,status,flags FROM records WHERE status!='ready' ORDER BY metadata_path"):
            json.dump(dict(row), handle, separators=(",", ":"), ensure_ascii=True)
            handle.write("\n")
        for row in connection.execute("SELECT * FROM problems ORDER BY id"):
            json.dump(dict(row), handle, separators=(",", ":"), ensure_ascii=True)
            handle.write("\n")
        for (path,) in connection.execute("SELECT path FROM files WHERE domain='data/silhouettes' AND lower(path) LIKE '%.png' AND NOT EXISTS(SELECT 1 FROM records WHERE png_path=files.path) ORDER BY path"):
            json.dump({"path": path, "category": "png-without-record", "status": "orphan"}, handle)
            handle.write("\n")
        for row in connection.execute("SELECT * FROM indexed WHERE metadata_path IS NULL OR NOT EXISTS(SELECT 1 FROM records WHERE records.metadata_path=indexed.metadata_path)"):
            json.dump({"category": "index-row-without-record", **dict(row)}, handle)
            handle.write("\n")
    with (output / "slug-collisions.jsonl").open("w", encoding="utf-8") as handle:
        for row in connection.execute("SELECT * FROM slug_collisions ORDER BY slug"):
            labels = [item[0] for item in connection.execute("SELECT label FROM labels WHERE slug=? ORDER BY label", (row["slug"],))]
            json.dump({"slug": row["slug"], "labels": labels}, handle, ensure_ascii=True)
            handle.write("\n")
    with (output / "identity-collisions.jsonl").open("w", encoding="utf-8") as handle:
        for row in connection.execute("SELECT * FROM identity_collisions ORDER BY instance_id"):
            members = [dict(item) for item in connection.execute("SELECT metadata_path,field,label,assignment,destination FROM records WHERE instance_id=? ORDER BY metadata_path", (row["instance_id"],))]
            json.dump({"instance_id": row["instance_id"], "members": members}, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
    with (output / "destination-collisions.jsonl").open("w", encoding="utf-8") as handle:
        for row in connection.execute("SELECT * FROM destination_collisions ORDER BY destination"):
            members = [dict(item) for item in connection.execute("SELECT metadata_path,field,label,assignment FROM records WHERE destination=? ORDER BY metadata_path", (row["destination"],))]
            json.dump({"destination": row["destination"], "members": members}, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
    with (output / "visual-ambiguities.jsonl").open("w", encoding="utf-8") as handle:
        for row in connection.execute("SELECT * FROM visual_ambiguities ORDER BY visual_key"):
            members = [dict(item) for item in connection.execute("SELECT records.metadata_path,records.field,records.label,records.assignment,files.sha256 AS png_sha256 FROM records LEFT JOIN files ON records.png_path=files.path WHERE records.visual_key=? ORDER BY records.metadata_path", (row["visual_key"],))]
            json.dump({"shot_frame_bbox_area": json.loads(row["visual_key"]), "members": members, "interpretation": "Potential visual duplication, not an automatic equivalence or repair"}, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
    cleanup = {}
    for media in ("movie", "gameplay"):
        root = project / "data/engravings/catalog" / media
        cleanup[media] = plan_empty_catalog_parents(root) if root.is_dir() else []
    artifact(output / "engraving-cleanup-analysis.json", {"read_only": True, "empty_catalog_parent_candidates": cleanup, "cause": "apply_removal prunes only immediate mode parents; intentionally stops above them", "proposed_fix": "After successful deletion, ascend using rmdir only while empty; stop at media catalog root; never follow symlinks or remove nonempty provenance. The read-only helper simulates this policy; no executor changed."})
    performance["export_seconds"] = time.perf_counter() - started
    performance["manifest_bytes"] = (output / "migration-plan.json").stat().st_size
    connection.row_factory = None


def verify_after(project: Path, output: Path, connection: sqlite3.Connection, performance: dict) -> dict:
    started = time.perf_counter()
    connection.execute("PRAGMA busy_timeout=300000")
    connection.execute("UPDATE files SET seen=0")
    connection.commit()
    changes = []
    count = 0
    for path in protected_files(project, output):
        relative = path.relative_to(project).as_posix()
        info, _ = fingerprint(path, retain_document=False)
        old = connection.execute("SELECT sha256,size,mtime_ns,kind FROM files WHERE path=?", (relative,)).fetchone()
        current = (info["sha256"], info["size"], info["mtime_ns"], info["kind"])
        if old != current:
            changes.append({"path": relative, "before": old, "after": current})
        connection.execute("UPDATE files SET seen=1 WHERE path=?", (relative,))
        count += 1
        if count % 25000 == 0:
            connection.commit()
            print(f"Safety verification: {count:,} files, {time.perf_counter() - started:.1f}s", flush=True)
    missing = [row[0] for row in connection.execute("SELECT path FROM files WHERE seen=0")]
    connection.commit()
    performance["after_seconds"] = time.perf_counter() - started
    performance["fingerprint_stat_calls_after"] = count * 2
    performance["hash_file_reads_after"] = count
    result = {"files_checked": count, "changed_files": changes, "missing_files": missing, "zero_protected_file_changes": not changes and not missing, "before_inventory_digest": inventory_digest(connection), "hash_algorithm": "SHA256", "timestamps": "mtime_ns; read-induced atime is not a mutation check", "manifest_scope": "All files under project data, outputs excluding isolated report root, and preferences", "per_file_evidence": "census.sqlite3:files"}
    artifact(output / "safety-after.json", result)
    return result


def complete_reference_audit(project_path: str | Path) -> Path:
    project = Path(project_path).resolve()
    output = project / "outputs/tests/silhouette-identity-migration"
    if any(path.is_symlink() for path in (project / "outputs", output.parent, output)):
        raise ValueError("Report ancestors cannot be symlinks")
    os.environ["SQLITE_TMPDIR"] = str(output / "sqlite-temp")
    connection = sqlite3.connect((output / "census.sqlite3").as_uri() + "?mode=rw", uri=True, timeout=300)
    connection.execute("PRAGMA cache_size=-32768")
    connection.execute("PRAGMA temp_store_directory='" + str(output / "sqlite-temp").replace("'", "''") + "'")
    started = time.perf_counter()
    try:
        baseline = json.loads((output / "safety-before.json").read_text())
        if not baseline.get("complete") or inventory_digest(connection) != baseline["digest"]:
            raise ValueError("Existing complete baseline does not match the census inventory")
        connection.execute("CREATE TABLE IF NOT EXISTS audited_csv(path TEXT PRIMARY KEY, rows INTEGER)")
        paths = connection.execute("SELECT path,sha256,size,mtime_ns,kind FROM files WHERE lower(path) LIKE '%.csv' AND path NOT IN(SELECT path FROM audited_csv) ORDER BY path").fetchall()
        for relative, sha256, size, mtime, kind in paths:
            path = project / relative
            current, _ = fingerprint(path, retain_document=False)
            if (current["sha256"], current["size"], current["mtime_ns"], current["kind"]) != (sha256, size, mtime, kind):
                raise ValueError(f"Protected CSV differs from baseline: {relative}")
            count = collect_csv_references(connection, project, path)
            connection.execute("INSERT INTO audited_csv VALUES(?,?)", (relative, count))
            connection.commit()
            print(f"CSV reference audit: {relative}, {count:,} rows", flush=True)
        summary = json.loads((output / "summary.json").read_text())
        summary["references"] = connection.execute("SELECT count(*) FROM refs").fetchone()[0]
        summary["reference_kinds"] = dict(connection.execute("SELECT kind,count(*) FROM refs GROUP BY kind"))
        summary["reference_subsystems"] = dict(connection.execute("SELECT subsystem,count(*) FROM refs GROUP BY subsystem"))
        summary["unresolved_filename_references"] = connection.execute("SELECT count(*) FROM refs WHERE normalized IS NULL").fetchone()[0]
        summary["csv_files_audited"], summary["csv_rows_audited"] = connection.execute("SELECT count(*),sum(rows) FROM audited_csv").fetchone()
        absent = connection.execute("SELECT normalized,count(*) FROM refs WHERE normalized IS NOT NULL AND NOT EXISTS(SELECT 1 FROM files WHERE files.path=refs.normalized) GROUP BY normalized").fetchall()
        summary["directory_reference_occurrences"] = sum(count for relative, count in absent if (project / relative).is_dir())
        summary["references_to_absent_files"] = sum(count for relative, count in absent if not (project / relative).exists())
        artifact(output / "summary.json", summary)
        performance = json.loads((output / "performance.json").read_text())
        performance["csv_reference_audit_seconds"] = time.perf_counter() - started
        export_reports(project, output, connection, summary, performance)
        artifact(output / "performance.json", performance)
    finally:
        connection.close()
    return resume_verification(project)


def resume_verification(project_path: str | Path) -> Path:
    project = Path(project_path).resolve()
    output = project / "outputs/tests/silhouette-identity-migration"
    if any(path.is_symlink() for path in (project / "outputs", output.parent, output)):
        raise ValueError("Report ancestors cannot be symlinks")
    baseline = json.loads((output / "safety-before.json").read_text())
    summary = json.loads((output / "summary.json").read_text())
    database = output / "census.sqlite3"
    connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=300)
    connection.execute("PRAGMA cache_size=-32768")
    lock = project / "data/silhouettes/.catalog-extraction.lock"
    lock_handle = lock.open("rb") if lock.is_file() else None
    performance_path = output / "performance.json"
    performance = json.loads(performance_path.read_text()) if performance_path.is_file() else dict(baseline["performance"])
    performance["verification_resumed"] = True
    started = time.perf_counter()
    try:
        if lock_handle:
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        if not baseline.get("complete") or inventory_digest(connection) != baseline["digest"]:
            raise ValueError("Existing complete baseline does not match the census inventory")
        missing = connection.execute("SELECT normalized,count(*) FROM refs WHERE normalized IS NOT NULL AND NOT EXISTS(SELECT 1 FROM files WHERE files.path=refs.normalized) GROUP BY normalized").fetchall()
        directory_refs = sum(count for relative, count in missing if (project / relative).is_dir())
        summary["directory_reference_occurrences"] = directory_refs
        summary["references_to_absent_files"] = sum(count for relative, count in missing if not (project / relative).exists())
        artifact(output / "summary.json", summary)
        safety = verify_after(project, output, connection, performance)
        performance.update({"resume_seconds": time.perf_counter() - started, "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "working_database_bytes": database.stat().st_size, "manifest_bytes": (output / "migration-plan.json").stat().st_size})
        if "index_scan_seconds" not in performance:
            performance["timing_limitations"] = "Initial index, analysis and export timings were not persisted before interruption; baseline and resumed verification timings are measured. Peak RSS here covers the resume process, not the initial process."
        artifact(performance_path, performance)
        write_report(output, summary, performance, safety)
        if not safety["zero_protected_file_changes"]:
            raise RuntimeError("Protected files changed; do not authorize migration")
    finally:
        connection.close()
        if lock_handle:
            lock_handle.close()
    return output


def write_report(output: Path, summary: dict, performance: dict, safety: dict) -> None:
    lines = ["# Silhouette Identity Census and Migration Plan", "", "Read-only. No migration executor exists. Identity allocation policy is NOT approved.", "", "## 1. Current Corpus", "", "```json", json.dumps(summary, indent=2, ensure_ascii=True), "```", "", "## 2. Current Identity Model", "", "object_NNNN is allocated by max existing four-digit PNG ordinal + 1 in catalog/<media>/<film>/<safe-label>, across shots. It is neither a shot ordinal nor annotation ordering nor a global ID. Multiple SAM masks from a request receive consecutive ordinals. Reindexing does not renumber assets; force/recheck extraction can append new records. The per-shot cache checks shot ID but not field in a shared label directory. A saved object has one original field/label/source-shot request; multiple historical predecessors can link to shared replacement objects.", "", "## 3. Proposed Identity Model", "", "shot#legacy-ordinal collisions are measured above. Even collision-free cases are NOT registered visual instances. The minimal compatible future token is shot#persisted-shot-wide-instance-ordinal, allocated once under an explicit registry across all labels/fields/extraction histories, NOT copied from object_NNNN. Classification corrections must retain that registered token. Re-extraction should produce a new identity or explicit predecessor linkage unless human evidence proves identity. Exact semantic duplicates cannot be inferred from ordinal equality. Shot-boundary edits also require explicit lineage.", "", "## 4. Slug Model", "", f"{SLUG_VERSION}: NFC Unicode normalization, lowercase, trim, underscore/whitespace/punctuation-to-hyphen, repeated separator collapse. Unicode letters/numbers are retained; no transliteration or synonym substitution. Original labels remain in the manifest. Empty/colliding slugs are never arbitrarily resolved. Full collision memberships are in slug-collisions.jsonl.", "", "## 5. Proposed Filesystem", "", "data/silhouettes/<media>/<field>/<label-slug>/<registered-shot-instance>/silhouette.{png,json}", "", "No catalog directory. Candidate instance IDs in migration-plan.json are collision-audit probes, not approved allocation. The bird candidate tmdb_61044@f078383-f078476#0002 can represent a shared silhouette/engraving identity only if explicitly registered and uniqueness/lineage decisions confirm it; object_0002 alone does not justify it.", "", "Future engraving path: data/engravings/movie/animals/bird/<registered-instance>/engraving.{png,json}, with runs/<backend>/run-NNN. Existing isolated/frame modes are distinct variants in the current Illustration contract; that contract must be resolved explicitly before collapsing canonical assets into one filename. This report does not authorize either identity renumbering or mode collapse.", "", "## 6. Migration Funnel", "", "See counts/statuses above. Ready=0 until a stable instance registry/allocation and collision/slug/lineage policy are approved. All active, inactive and superseded records remain represented. Collision, orphan, ambiguity and structural counts overlap; statuses are a prioritized summary, not an additive partition of independent issues.", "", "## 7. Reference Audit and Downstream Assumptions", "", "references.jsonl contains JSON pointers and SQLite row identities, original absolute/relative/filename/identity references, and normalized project paths. Foreign absolute roots are normalized only for matching; their stored values are never rewritten. migration-plan.json associates reference IDs with each old PNG/JSON pair.", "", "- silhouette_catalog: catalog_item_dir, _safe_label, _next_object_index and per-shot cache assume title/label directories and object_NNNN files.", "- illustration_index: scanner and IDs/payload paths depend on catalog layout and file stems; normal active discovery relies on the ready index, which was not rebuilt.", "- silhouette_discovery/reference/curation/recheck/provenance: exact path and legacy catalog identities, historical predecessor/replacement links and browse-index authorization need compatibility handling.", "- engraving_paths, engraving_local_paths/publish/lifecycle: roots and provenance derive film/label/object_id; local runs are mode-aware and books may block removal via absolute references.", "- annotation_qa_qwen_vl and engraving_benchmark: persisted absolute/relative record_path and PNG basename/source metadata require read-only identity resolvers before relocation.", "- Book: imported illustrations are copied to book-local relative sources; copied pixels should stay untouched. Absolute external dependencies and any retained provenance pointers need reference-aware handling.", "- annotations/shotlists: shot IDs and field values are canonical upstream references, not a source to rewrite to match filesystem policy. Current shot membership is reported, including historical shots not in current shotlists.", "", "## 8. Lifecycle Cleanup Bug", "", "engraving_lifecycle.apply_removal intentionally removes only immediate empty mode parents, leaving empty object/label/film scaffolding. The proposed tested fix is a boundary-limited empty-only ancestor ascent after successful removals, never rmtree on ancestors, stopping at the media catalog root and retaining nonempty provenance or book dependencies. Only a virtual read-only planner was added; apply_removal is unchanged. Empty hierarchy candidates are listed in engraving-cleanup-analysis.json.", "", "## 9. Performance", "", "```json", json.dumps(performance, indent=2), "```", "", "The process streams files/individual JSON documents and uses disk-backed SQLite grouping; it never decodes PNGs. Large reports are streamed JSON/JSONL. stat/read counts refer to explicit fingerprint operations, not every kernel call made internally by Python/SQLite. Per-file hashes and mtimes are retained in census.sqlite3.", "", "## 10. Safety", "", "```json", json.dumps(safety, indent=2), "```", "", "## 11. Recommendation", "", "NOT ready for --apply. Resolve stable shot-wide registration, duplicate/re-extraction lineage, scoped slug collisions and reference compatibility first. No production migration, deletion, index rebuild or QA integration was implemented or executed. Stop for human review."]
    overview = ["| Measure | Count |", "| --- | ---: |"]
    for key in ("physical_silhouette_files", "silhouette_pngs", "silhouette_jsons", "records", "sources", "labels", "unique_slugs", "identity_collision_groups", "identity_collision_records", "destination_collision_groups", "slug_collision_groups", "missing_identity_records", "missing_png_records", "pngs_without_records", "records_not_indexed", "index_rows_without_records", "ready"):
        overview.append(f"| {key} | {summary[key]:,} |")
    overview.extend(["", "Lifecycle: " + ", ".join(f"{state}={count:,}" for state, count in summary["assignment"].items()) + ".", "", "Media: " + ", ".join(f"{media}={count:,}" for media, count in summary["media"].items()) + ".", "", "Fields: " + ", ".join(f"{field or '<missing>'}={count:,}" for field, count in summary["fields"].items()) + ".", "", "Complete source populations, samples and diagnostics remain in summary.json and census.sqlite3.", "", "| Media | Field | Label | Shot | Old ID |", "| --- | --- | --- | --- | --- |"])
    sampled = set()
    for sample in summary["samples"]:
        key = (sample["media_type"], sample["field"])
        if sample["assignment"] != "active" or not sample["field"] or key in sampled:
            continue
        sampled.add(key)
        overview.append("| " + " | ".join(str(sample[name]).replace("|", "\\|") for name in ("media_type", "field", "label", "shot_id", "object_id")) + " |")
    opening = lines.index("```json")
    closing = lines.index("```", opening)
    lines[opening:closing + 1] = overview
    compatibility = ["", "| Owner | Required future change, not implemented |", "| --- | --- |", "| silhouette_catalog | Replace film/label path allocator and four-digit globs with explicit registered identity; keep extraction and lifecycle semantics shared. |", "| illustration_index | Replace catalog-root invalidation, parent-derived identity and source-stem fallback; rebuild only in a separately authorized migration. |", "| silhouette_discovery / MCP / Illustration / Mosaic | Preserve active-index authorization and lifecycle filtering; resolve candidate IDs through a canonical alias/locator, not a second scan. |", "| engraving_paths / local paths / lifecycle | Stop treating source_json.stem as object identity or its parent as the label. Preserve source links, backend provenance and review state. |", "| benchmark / evidence / annotation QA | Keep frozen artifacts byte-identical. Use an external old-to-new resolver rather than rewriting prior evidence or pinned bird paths. |", "| Book | Preserve copied relative illustrations; audit exceptional absolute paths and provenance separately. |", "", "Backend remains provenance. The target has one engraving.png per instance, while current isolated/frame modes can coexist. A publication/variant policy must be explicitly approved before either can occupy that single slot."]
    lines[lines.index("## 8. Lifecycle Cleanup Bug"):lines.index("## 8. Lifecycle Cleanup Bug")] = compatibility + [""]
    reference_note = f"Reference occurrences: {summary['references']:,}; reconstructible legacy identities: {summary['reference_kinds'].get('identity_reconstructible', 0):,}. Repeated lifecycle links and their index mirrors are counted separately, not as unique objects. Existing directory references: {summary.get('directory_reference_occurrences', 0):,}; genuinely absent targets: {summary['references_to_absent_files']:,}. Book and annotation files were scanned even when they contained no recognized silhouette references. JSON/JSONL and relevant SQLite columns are structurally inspected; arbitrary prose and opaque binary references are not proven complete."
    lines.insert(lines.index("## 8. Lifecycle Cleanup Bug"), reference_note)
    cleanup = json.loads((output / "engraving-cleanup-analysis.json").read_text())
    lines.insert(lines.index("## 9. Performance"), "Empty-only cleanup candidates: " + ", ".join(f"{media}={len(paths):,}" for media, paths in cleanup["empty_catalog_parent_candidates"].items()) + ". No directory was removed.")
    lines.insert(lines.index("## 11. Recommendation"), f"Protected files verified: {safety['files_checked']:,}; zero changes: {safety['zero_protected_file_changes']}. This checks SHA256, size and mtime_ns, not read-induced atime.")
    if "path_lengths_bytes" in summary:
        lengths = summary["path_lengths_bytes"]
        lines.insert(lines.index("## 6. Migration Funnel"), f"Proposed absolute JSON path byte lengths: min={lengths['min']}, median={lengths['median']}, p95={lengths['p95']}, max={lengths['max']}; old silhouette maximum={summary['old_silhouette_full_path_max_bytes']}. Component/full-path limit violations: {lengths['component_over_255_records']}/{lengths['full_path_over_4096_records']}.")
    if summary.get("csv_files_audited"):
        lines.insert(lines.index("## 8. Lifecycle Cleanup Bug"), f"CSV extension: {summary['csv_files_audited']:,} files / {summary['csv_rows_audited']:,} rows streamed, including JSON-valued CSV cells. CSV references are included in each manifest record's reference IDs and in references.jsonl.")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_census(project_path: str | Path) -> Path:
    project = Path(project_path).resolve()
    output = project / "outputs/tests/silhouette-identity-migration"
    if not output.parent.is_dir() or any(path.is_symlink() for path in (project / "outputs", output.parent, output)):
        raise ValueError("Report parent must already exist and report ancestors cannot be symlinks")
    output.mkdir(exist_ok=True)
    database = output / "census.sqlite3"
    if database.exists():
        raise FileExistsError("Existing census retained; no overwrite or apply mode is supported")
    temp = output / "sqlite-temp"
    temp.mkdir()
    os.environ["SQLITE_TMPDIR"] = str(temp)
    connection = sqlite3.connect(database, timeout=300)
    connection.execute("PRAGMA cache_size=-32768")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA temp_store_directory='" + str(temp).replace("'", "''") + "'")
    create_tables(connection)
    started = time.perf_counter()
    performance = {"streams": True, "images_decoded": 0}
    lock = project / "data/silhouettes/.catalog-extraction.lock"
    lock_handle = lock.open("rb") if lock.is_file() else None
    try:
        if lock_handle:
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        scan_before(project, output, connection, performance)
        scan_indexes(project, connection, performance)
        summary = analyze(connection, project, performance)
        artifact(output / "summary.json", summary)
        export_reports(project, output, connection, summary, performance)
        artifact(output / "performance.json", performance)
        safety = verify_after(project, output, connection, performance)
        performance.update({"total_seconds": time.perf_counter() - started, "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "working_database_bytes": database.stat().st_size})
        artifact(output / "performance.json", performance)
        write_report(output, summary, performance, safety)
        if not safety["zero_protected_file_changes"]:
            raise RuntimeError("Protected files changed; do not authorize migration")
    finally:
        connection.close()
        if lock_handle:
            lock_handle.close()
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only full silhouette identity census; no apply mode")
    parser.add_argument("--project", required=True)
    parser.add_argument("--resume-verification", action="store_true", help="Verify an existing complete baseline without replacing census or mapping evidence")
    arguments = parser.parse_args()
    print(resume_verification(arguments.project) if arguments.resume_verification else run_census(arguments.project))