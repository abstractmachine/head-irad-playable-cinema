"""Derived browse indexes for the Illustration visualizer.

Canonical silhouette and engraving sidecars remain the source of truth. These
compact indexes exist only to avoid reconstructing a browse catalog from many
small files on every visualizer launch.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4


INDEX_SCHEMA_VERSION = 3
SOURCES = ("silhouettes", "engravings")
_SORT_COLUMNS = {
    "confidence": "confidence_score",
    "usefulness": "usefulness_score",
    "engraving": "engraving_score",
    "fullness": "fullness_score",
    "size": "size_score",
    "completeness": "completeness_score",
    "isolation": "isolation_score",
    "semantic_label": "semantic_label_score",
    "semantic_field": "semantic_field_score",
    "engraved_first": "engraved_score",
}


def _index_dir(project_path: str | Path) -> Path:
    return Path(project_path) / "data" / "indexes" / "illustration"


def index_path(project_path: str | Path, source: str, media_type: str) -> Path:
    _validate_source(source)
    return _index_dir(project_path) / f"{media_type}-{source}.sqlite3"


def _obsolete_index_paths(
    project_path: str | Path, source: str, media_type: str
) -> tuple[Path, ...]:
    base = _index_dir(project_path) / f"{media_type}-{source}"
    return (base.with_suffix(".jsonl"), base.with_suffix(".json"))


def _revision_path(project_path: str | Path, source: str, media_type: str) -> Path:
    _validate_source(source)
    return _index_dir(project_path) / f"{media_type}-{source}.revision"


def _validate_source(source: str) -> None:
    if source not in SOURCES:
        raise ValueError(f"Unknown Illustration index source: {source}")


def _read_revision(project_path: str | Path, source: str, media_type: str) -> str:
    path = _revision_path(project_path, source, media_type)
    try:
        return path.read_text(encoding="utf-8").strip() or "0"
    except FileNotFoundError:
        return "0"


def invalidate_index(project_path: str | Path, source: str, media_type: str) -> None:
    """Mark one derived index stale without scanning its canonical source tree."""
    from data.annotate import atomic_write_text

    path = _revision_path(project_path, source, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, uuid4().hex)


def invalidate_for_record(record_path: str | Path, source: str) -> bool:
    """Invalidate the source index inferred from a canonical record path."""
    path = Path(record_path).resolve()
    catalog_name = "silhouettes" if source == "silhouettes" else "engravings"
    parts = path.parts
    for index in range(len(parts) - 3):
        if parts[index:index + 3] == ("data", catalog_name, "catalog"):
            project = Path(*parts[:index])
            media_type = parts[index + 3]
            invalidate_index(project, source, media_type)
            return True
    return False


def load_index(
    project_path: str | Path,
    source: str,
    media_type: str,
) -> dict:
    """Return status and cheap metadata without loading catalog records."""
    project = Path(project_path)
    path = index_path(project, source, media_type)
    if not path.exists():
        if any(candidate.exists() for candidate in _obsolete_index_paths(
            project, source, media_type
        )):
            return {"status": "stale", "count": 0}
        return {"status": "missing", "count": 0}

    try:
        with sqlite3.connect(path) as connection:
            meta = dict(connection.execute("SELECT key, value FROM meta"))
        if int(meta.get("schema_version", -1)) != INDEX_SCHEMA_VERSION:
            return {"status": "stale", "count": 0}
        if meta.get("revision") != _read_revision(project, source, media_type):
            return {"status": "stale", "count": 0}
        return {
            "status": "ready",
            "count": int(meta.get("record_count", 0)),
            "generated_at": meta.get("generated_at"),
        }
    except Exception as exc:
        return {"status": "error", "count": 0, "error": str(exc)}


def rebuild_index(project_path: str | Path, source: str, media_type: str) -> dict:
    """Rebuild one source index from canonical sidecars."""
    _validate_source(source)
    project = Path(project_path)
    revision = _read_revision(project, source, media_type)
    if source == "silhouettes":
        records = _scan_silhouettes(project, media_type)
    else:
        records = _scan_engravings(project, media_type)

    if revision != _read_revision(project, source, media_type):
        return {"status": "stale", "count": 0}

    path = index_path(project, source, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with sqlite3.connect(temporary) as connection:
            _create_schema(connection)
            connection.executemany(
                """INSERT INTO records (
                    title, field, initial, label, mode, object_id, human_best,
                    confidence_score, usefulness_score, engraving_score,
                    fullness_score, size_score, completeness_score,
                    isolation_score, semantic_label_score, semantic_field_score,
                    engraved_score, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_record_row(project, record) for record in records),
            )
            meta = {
                "schema_version": str(INDEX_SCHEMA_VERSION),
                "source": source,
                "media_type": media_type,
                "revision": revision,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "record_count": str(len(records)),
            }
            connection.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)", meta.items()
            )
            connection.commit()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    for obsolete in _obsolete_index_paths(project, source, media_type):
        obsolete.unlink(missing_ok=True)
    return {"status": "ready", "count": len(records), "path": path}


def rebuild_all(project_path: str | Path, media_type: str) -> dict:
    """Rebuild both source indexes for one media type."""
    return {
        source: rebuild_index(project_path, source, media_type)
        for source in SOURCES
    }


def query_facets(
    project_path: str | Path,
    source: str,
    media_type: str,
    *,
    title: str | None = None,
    field: str | None = None,
    letter: str | None = None,
    mode: str | None = None,
) -> dict:
    """Return distinct facets and label counts for a browse scope."""
    status = load_index(project_path, source, media_type)
    if status["status"] != "ready":
        return {**status, "titles": [], "fields": [], "letters": [], "labels": []}
    where, params = _where(title=title, field=field, letter=letter, mode=mode)
    with sqlite3.connect(index_path(project_path, source, media_type)) as connection:
        titles = [row[0] for row in connection.execute(
            "SELECT DISTINCT title FROM records ORDER BY title COLLATE NOCASE"
        )]
        fields = [row[0] for row in connection.execute(
            f"SELECT DISTINCT field FROM records {where} ORDER BY field COLLATE NOCASE",
            params,
        )]
        letters = [row[0] for row in connection.execute(
            f"SELECT DISTINCT initial FROM records {where} ORDER BY initial COLLATE NOCASE",
            params,
        )]
        labels = [
            {"label": row[0], "count": row[1]}
            for row in connection.execute(
                f"SELECT label, COUNT(*) FROM records {where} "
                "GROUP BY label ORDER BY label COLLATE NOCASE",
                params,
            )
        ]
    return {**status, "titles": titles, "fields": fields, "letters": letters, "labels": labels}


def query_page(
    project_path: str | Path,
    source: str,
    media_type: str,
    *,
    title: str | None = None,
    field: str | None = None,
    letter: str | None = None,
    label: str | None = None,
    mode: str | None = None,
    object_id: str | None = None,
    human_best: bool | None = None,
    sort_keys: list[str] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """Return one materialized browse page and its matching total count."""
    status = load_index(project_path, source, media_type)
    if status["status"] != "ready":
        return {**status, "total": 0, "records": []}
    where, params = _where(
        title=title, field=field, letter=letter, label=label, mode=mode,
        object_id=object_id, human_best=human_best,
    )
    with sqlite3.connect(index_path(project_path, source, media_type)) as connection:
        total = int(connection.execute(
            f"SELECT COUNT(*) FROM records {where}", params
        ).fetchone()[0])
        rows = connection.execute(
            f"SELECT payload FROM records {where} {_order_by(sort_keys or [])} "
            "LIMIT ? OFFSET ?",
            [*params, max(1, int(limit)), max(0, int(offset))],
        )
        records = [_deserialize_record(Path(project_path), row[0]) for row in rows]
    return {**status, "total": total, "records": records}


def query_records(
    project_path: str | Path,
    source: str,
    media_type: str,
    *,
    limit: int = 100_000,
    **filters,
) -> list[dict]:
    """Return a deliberately bounded record set for an action workflow."""
    return query_page(
        project_path, source, media_type, limit=limit, **filters
    ).get("records", [])


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE records (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            field TEXT NOT NULL,
            initial TEXT NOT NULL,
            label TEXT NOT NULL,
            mode TEXT NOT NULL,
            object_id TEXT NOT NULL,
            human_best INTEGER NOT NULL,
            confidence_score REAL NOT NULL,
            usefulness_score REAL NOT NULL,
            engraving_score REAL NOT NULL,
            fullness_score REAL NOT NULL,
            size_score REAL NOT NULL,
            completeness_score REAL NOT NULL,
            isolation_score REAL NOT NULL,
            semantic_label_score REAL NOT NULL,
            semantic_field_score REAL NOT NULL,
            engraved_score REAL NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX records_title ON records(title);
        CREATE INDEX records_field ON records(field);
        CREATE INDEX records_initial ON records(initial);
        CREATE INDEX records_label ON records(label);
        CREATE INDEX records_mode ON records(mode);
        CREATE INDEX records_filters ON records(title, field, initial, label, mode);
        """
    )


def _record_row(project: Path, record: dict) -> tuple:
    title = _clean_stem(record.get("filename_stem", ""))
    field = str(record.get("field") or "--all")
    label = str(record.get("label") or "")
    first = label[:1]
    initial = first.upper() if first.isalpha() else "#"
    record_path = Path(str(record.get("path") or ""))
    object_id = str(record.get("object_id") or record_path.stem)
    return (
        title, field, initial, label, str(record.get("mode") or ""),
        object_id, 1 if record.get("human_best") else 0,
        _numeric_score(record, "confidence"), _numeric_score(record, "usefulness"),
        _numeric_score(record, "engraving"), _numeric_score(record, "fullness"),
        _numeric_score(record, "size"), _numeric_score(record, "completeness"),
        _numeric_score(record, "isolation"), _numeric_score(record, "semantic_label"),
        _numeric_score(record, "semantic_field"), float(bool(record.get("engraved"))),
        json.dumps(
            _serialize_record(project, record), ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def _numeric_score(record: dict, key: str) -> float:
    value = record.get(f"{key}_score")
    if value is None:
        value = record.get(key)
    try:
        if value is not None:
            return float(value)
    except (TypeError, ValueError):
        pass
    mask_area = record.get("mask_area")
    if key == "fullness" and mask_area is not None:
        bbox = record.get("bbox") or []
        if len(bbox) >= 4:
            return max(0.0, min(1.0, float(mask_area) / max(1.0, bbox[2] * bbox[3])))
    if key == "size" and mask_area is not None:
        frame_size = record.get("frame_size") or []
        if len(frame_size) >= 2:
            area = float(mask_area) / max(1.0, frame_size[0] * frame_size[1])
            return max(0.0, min(1.0, (area - 0.002) / 0.298))
    return 0.0


def _where(**filters) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    columns = {
        "title": "title", "field": "field", "letter": "initial",
        "label": "label", "mode": "mode", "object_id": "object_id",
    }
    for name, column in columns.items():
        value = filters.get(name)
        if value not in (None, "", "--all"):
            clauses.append(f"{column} = ?")
            params.append(value)
    if filters.get("human_best") is not None:
        clauses.append("human_best = ?")
        params.append(1 if filters["human_best"] else 0)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _order_by(sort_keys: list[str]) -> str:
    numeric = [_SORT_COLUMNS[key] for key in sort_keys if key in _SORT_COLUMNS]
    if numeric:
        return f"ORDER BY ({' + '.join(numeric)}) / {len(numeric)} DESC, id"
    if "alphabetical" in sort_keys:
        return "ORDER BY label COLLATE NOCASE, id"
    return "ORDER BY id"


def _serialize_record(project: Path, record: dict) -> dict:
    serialized = dict(record)
    for key in ("path", "output_png", "raw_png"):
        value = serialized.get(key)
        if not value:
            continue
        path = Path(value)
        try:
            serialized[key] = str(path.relative_to(project))
        except ValueError:
            serialized[key] = str(path)
    return serialized


def _deserialize_record(project: Path, payload: str) -> dict:
    record = json.loads(payload)
    for key in ("path", "output_png", "raw_png"):
        value = record.get(key)
        if value:
            path = Path(value)
            record[key] = path if path.is_absolute() else project / path
    return record


def _clean_stem(stem: str) -> str:
    return re.sub(r"\s*\{tmdb-\d+\}", "", str(stem)).strip()


def _scan_silhouettes(project: Path, media_type: str) -> list[dict]:
    from services.silhouette_catalog import scan_catalog

    records = [
        record for record in scan_catalog(str(project), media_type=media_type)
        if "error" not in record
    ]
    engraved = _engraved_source_keys(project, media_type)
    for record in records:
        path = Path(str(record.get("path") or ""))
        key = (path.parent.parent.name, path.parent.name, path.stem)
        record["engraved"] = key in engraved
    return records


def _engraved_source_keys(project: Path, media_type: str) -> set[tuple[str, str, str]]:
    from services.engraving_paths import read_engraving_meta

    base = project / "data" / "engravings" / "catalog" / media_type
    keys: set[tuple[str, str, str]] = set()
    if not base.is_dir():
        return keys
    for metadata_path in base.glob("*/*/*/isolated/engraving.json"):
        metadata = read_engraving_meta(metadata_path)
        if metadata and metadata.get("status") == "generated":
            mode_dir = metadata_path.parent
            keys.add((
                mode_dir.parents[2].name,
                mode_dir.parents[1].name,
                mode_dir.parent.name,
            ))
    return keys


def _scan_engravings(project: Path, media_type: str) -> list[dict]:
    from services.engraving_paths import read_engraving_meta

    base = project / "data" / "engravings" / "catalog" / media_type
    if not base.is_dir():
        return []

    records: list[dict] = []
    for metadata_path in sorted(base.rglob("engraving.json")):
        metadata = read_engraving_meta(metadata_path)
        if not metadata or metadata.get("status") != "generated":
            continue
        try:
            mode_dir = metadata_path.parent
            label_dir = mode_dir.parent.parent
            film_dir = label_dir.parent
            raw_path = mode_dir / "raw.png"
            raw_png = raw_path if raw_path.exists() else None
            output_png = Path(str(metadata.get("output_png", ""))) if metadata.get("output_png") else None
            if output_png and not output_png.is_absolute():
                output_png = project / output_png
            if not output_png or not output_png.exists():
                named = [path for path in mode_dir.glob("*.png") if path.name != "raw.png"]
                output_png = named[0] if named else raw_png

            record = {
                "label": label_dir.name,
                "field": "--all",
                "filename_stem": film_dir.name,
                "media_type": media_type,
                "mode": mode_dir.name,
                "object_id": mode_dir.parent.name,
                "output_png": output_png,
                "raw_png": raw_png,
                "path": metadata_path,
            }
            for key, value in metadata.items():
                if key not in record and not isinstance(value, (dict, list)):
                    record[key] = value
            records.append(record)
        except Exception:
            continue
    return records