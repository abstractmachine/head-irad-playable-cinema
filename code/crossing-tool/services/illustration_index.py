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


INDEX_SCHEMA_VERSION = 5
SOURCES = ("silhouettes", "engravings")

# ---------------------------------------------------------------------------
# Explicit query-state vocabulary
# ---------------------------------------------------------------------------
# Three distinct states recur throughout the Illustration browse stack and
# must never be conflated:
#
#   UNINITIALIZED  a falsy media_type (None/"").  No index is loaded, no
#                  query is ever issued.  Purely a construction-time /
#                  fast-empty-browser state (see IllustrationBrowser.reload).
#   ALL_MEDIA      a real, truthy sentinel meaning "query every supported
#                  media type and merge the results".  This is a genuine
#                  cross-index query, never treated as "nothing selected".
#   <media type>   one concrete, real value from MEDIA_TYPES ("movie" or
#                  "gameplay") — a single-index query.
#
# ALL is the equivalent, pre-existing sentinel for the *other* filter
# dimensions (title/field/letter/label/mode/object_id): "no restriction on
# this column". It has always been the literal string "--all"; it is named
# here so call sites reference one constant instead of retyping the literal.
MEDIA_TYPES: tuple[str, ...] = ("movie", "gameplay")
ALL_MEDIA = "--all-media--"
ALL = "--all"
UNTYPED_FIELD = "<untyped>"

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
    """Return status and cheap metadata without loading catalog records.

    ``media_type == ALL_MEDIA`` merges status across every entry in
    ``MEDIA_TYPES`` rather than reading one on-disk index.
    """
    if media_type == ALL_MEDIA:
        return _load_index_all_media(project_path, source)
    project = Path(project_path)
    path = index_path(project, source, media_type)
    if not path.exists():
        if any(candidate.exists() for candidate in _obsolete_index_paths(
            project, source, media_type
        )):
            return {"status": "stale", "count": 0, "usable": False}
        return {"status": "missing", "count": 0}

    try:
        with sqlite3.connect(path) as connection:
            meta = dict(connection.execute("SELECT key, value FROM meta"))
        if int(meta.get("schema_version", -1)) != INDEX_SCHEMA_VERSION:
            return {"status": "stale", "count": 0, "usable": False}
        details = {
            "count": int(meta.get("record_count", 0)),
            "generated_at": meta.get("generated_at"),
            "usable": True,
        }
        if meta.get("revision") != _read_revision(project, source, media_type):
            return {"status": "stale", **details}
        return {
            "status": "ready",
            **details,
        }
    except Exception as exc:
        return {"status": "error", "count": 0, "error": str(exc)}


def _load_index_all_media(project_path: str | Path, source: str) -> dict:
    """Merge per-media-type status into one ALL_MEDIA status.

    Usable whenever at least one media type's index is usable \u2014 a genuinely
    missing/stale gameplay index must never block browsing movie results
    under <All Media>, and vice versa.
    """
    statuses = {mt: load_index(project_path, source, mt) for mt in MEDIA_TYPES}
    usable = {mt: s for mt, s in statuses.items() if s.get("usable")}
    if usable:
        return {
            "status": "stale" if any(s.get("status") == "stale" for s in usable.values()) else "ready",
            "count": sum(int(s.get("count", 0)) for s in usable.values()),
            "usable": True,
        }
    for candidate in ("error", "stale", "missing"):
        match = next((s for s in statuses.values() if s.get("status") == candidate), None)
        if match is not None:
            return {**match, "count": 0}
    return {"status": "missing", "count": 0}


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
                        engraved_score, search_provenance_state,
                        search_provenance_reason, search_provenance_audit_version,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    provenance_state: str | None = None,
) -> dict:
    """Return distinct facets and label counts for a browse scope.

    ``media_type == ALL_MEDIA`` merges facets across every entry in
    ``MEDIA_TYPES``.
    """
    if media_type == ALL_MEDIA:
        return _query_facets_all_media(
            project_path, source, title=title, field=field, letter=letter, mode=mode,
        )
    status = load_index(project_path, source, media_type)
    if not status.get("usable"):
        return {**status, "titles": [], "fields": [], "letters": [], "labels": []}
    where, params = _where(
        title=title,
        field=field,
        letter=letter,
        mode=mode,
        provenance_state=provenance_state,
    )
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


def query_field_counts(
    project_path: str | Path,
    source: str,
    media_type: str,
) -> dict:
    """Return a validated field distribution from one usable browse index."""
    status = load_index(project_path, source, media_type)
    if not status.get("usable"):
        result = {
            "status": status.get("status") or "error",
            "count": 0,
            "fields": [],
        }
        if status.get("status") == "stale":
            result["usable"] = False
        return result

    try:
        with sqlite3.connect(index_path(project_path, source, media_type)) as connection:
            rows = connection.execute(
                "SELECT field, COUNT(*) FROM records "
                "GROUP BY field ORDER BY COUNT(*) DESC, field"
            )
            fields = []
            for field, count in rows:
                name = str(field)
                if name == ALL and source == "silhouettes":
                    fields.append({
                        "field": UNTYPED_FIELD,
                        "count": int(count),
                        "synthetic": True,
                    })
                else:
                    fields.append({"field": name, "count": int(count)})
    except Exception:
        return {"status": "error", "count": 0, "fields": []}

    if any(not item["field"] or item["field"] == ALL for item in fields):
        return {"status": "error", "count": 0, "fields": []}
    fields.sort(key=lambda item: (-item["count"], item["field"], item.get("synthetic", False)))
    count = sum(item["count"] for item in fields)
    if count != int(status.get("count", 0)):
        return {"status": "error", "count": 0, "fields": []}
    return {"status": status["status"], "count": count, "fields": fields}


def _query_facets_all_media(project_path: str | Path, source: str, **filters) -> dict:
    """Merge ``query_facets`` results across every entry in ``MEDIA_TYPES``."""
    status = _load_index_all_media(project_path, source)
    if not status.get("usable"):
        return {**status, "titles": [], "fields": [], "letters": [], "labels": []}
    titles: set[str] = set()
    fields: set[str] = set()
    letters: set[str] = set()
    label_counts: dict[str, int] = {}
    for mt in MEDIA_TYPES:
        sub_status = load_index(project_path, source, mt)
        if not sub_status.get("usable"):
            continue
        sub = query_facets(project_path, source, mt, **filters)
        titles.update(sub.get("titles", []))
        fields.update(sub.get("fields", []))
        letters.update(sub.get("letters", []))
        for row in sub.get("labels", []):
            label_counts[row["label"]] = label_counts.get(row["label"], 0) + row["count"]
    return {
        **status,
        "titles": sorted(titles, key=str.casefold),
        "fields": sorted(fields, key=str.casefold),
        "letters": sorted(letters, key=str.casefold),
        "labels": [
            {"label": label, "count": count}
            for label, count in sorted(label_counts.items(), key=lambda kv: kv[0].casefold())
        ],
    }


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
    provenance_state: str | None = None,
    sort_keys: list[str] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """Return one materialized browse page and its matching total count.

    ``media_type == ALL_MEDIA`` merges pages across every entry in
    ``MEDIA_TYPES``: the pagination math (``total``/``offset``/``limit``) is
    always computed against the single, final, merged+sorted result set —
    never against one media type's result set alone.
    """
    if media_type == ALL_MEDIA:
        return _query_page_all_media(
            project_path, source,
            title=title, field=field, letter=letter, label=label, mode=mode,
            object_id=object_id, human_best=human_best,
            provenance_state=provenance_state,
            sort_keys=sort_keys,
            offset=offset, limit=limit,
        )
    status = load_index(project_path, source, media_type)
    if not status.get("usable"):
        return {**status, "total": 0, "records": []}
    where, params = _where(
        title=title, field=field, letter=letter, label=label, mode=mode,
        object_id=object_id, human_best=human_best,
        provenance_state=provenance_state,
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


# Generous fetch bound per media type when merging for an ALL_MEDIA page —
# these are compact derived indexes (see module docstring); a bounded-but-
# large fetch followed by an in-Python re-sort is the same "fetch a bounded
# complete set, then sort/slice" pattern query_records already establishes.
_ALL_MEDIA_FETCH_LIMIT = 1_000_000


def _query_page_all_media(
    project_path: str | Path,
    source: str,
    *,
    sort_keys: list[str] | None = None,
    offset: int = 0,
    limit: int = 50,
    **filters,
) -> dict:
    """Merge, re-sort, and paginate across every entry in ``MEDIA_TYPES``.

    Each media type's own on-disk ``id`` ordering has no cross-database
    meaning, so every matching record from every usable media type is fetched
    first, then re-sorted in Python using the same semantics as ``_order_by``
    (see ``_sort_merged_records``), and only then sliced to the requested
    page — the total/offset/limit are always computed against this single
    final merged+sorted result set, never against one media type alone.
    """
    status = _load_index_all_media(project_path, source)
    if not status.get("usable"):
        return {**status, "total": 0, "records": []}
    merged: list[dict] = []
    for mt in MEDIA_TYPES:
        sub_status = load_index(project_path, source, mt)
        if not sub_status.get("usable"):
            continue
        sub = query_page(
            project_path, source, mt, sort_keys=sort_keys,
            offset=0, limit=_ALL_MEDIA_FETCH_LIMIT, **filters,
        )
        merged.extend(sub.get("records", []))
    merged = _sort_merged_records(merged, sort_keys)
    start = max(0, int(offset))
    end = start + max(1, int(limit))
    return {**status, "total": len(merged), "records": merged[start:end]}


def _sort_merged_records(records: list[dict], sort_keys: list[str] | None) -> list[dict]:
    """Python-side equivalent of ``_order_by`` for merging already-sorted
    per-media-type record lists into one cross-media order.

    Reuses ``_numeric_score`` directly — deserialized page records retain the
    same raw scan-time fields (confidence, mask_area, bbox, frame_size, …)
    that function already knows how to read, so no separate scoring logic is
    needed for the merged, cross-database case.
    """
    keys = [key for key in (sort_keys or []) if key]
    numeric = [key for key in keys if key in _SORT_COLUMNS]
    if numeric:
        count = len(numeric)
        return sorted(
            records,
            key=lambda r: sum(_numeric_score(r, key) for key in numeric) / count,
            reverse=True,
        )
    if "alphabetical" in keys:
        return sorted(records, key=lambda r: str(r.get("label") or "").casefold())
    # No sort keys — preserve MEDIA_TYPES order, each sub-list's own id order.
    return records


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


def query_untyped_records(
    project_path: str | Path,
    source: str,
    media_type: str,
) -> dict:
    """Return every indexed record stored under the synthetic untyped field."""
    status = load_index(project_path, source, media_type)
    if not status.get("usable"):
        return {**status, "total": 0, "records": []}
    with sqlite3.connect(index_path(project_path, source, media_type)) as connection:
        rows = connection.execute(
            "SELECT payload FROM records WHERE field = ? ORDER BY id",
            (ALL,),
        )
        records = [
            _deserialize_record(Path(project_path), row[0])
            for row in rows
        ]
    return {**status, "total": len(records), "records": records}


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
            search_provenance_state TEXT,
            search_provenance_reason TEXT,
            search_provenance_audit_version TEXT,
            payload TEXT NOT NULL
        );
        CREATE INDEX records_title ON records(title);
        CREATE INDEX records_field ON records(field);
        CREATE INDEX records_initial ON records(initial);
        CREATE INDEX records_label ON records(label);
        CREATE INDEX records_mode ON records(mode);
        CREATE INDEX records_search_provenance_state ON records(search_provenance_state);
        CREATE INDEX records_filters ON records(title, field, initial, label, mode);
        """
    )


def _record_row(project: Path, record: dict) -> tuple:
    title = _clean_stem(record.get("filename_stem", ""))
    field = str(record.get("field") or ALL)
    label = str(record.get("label") or "")
    first = label[:1]
    initial = first.upper() if first.isalpha() else "#"
    record_path = Path(str(record.get("path") or ""))
    object_id = str(record.get("object_id") or record_path.stem)
    provenance = record.get("search_provenance") if isinstance(record.get("search_provenance"), dict) else {}
    provenance_state = provenance.get("state") if isinstance(provenance, dict) else None
    provenance_reason = provenance.get("reason") if isinstance(provenance, dict) else None
    provenance_version = provenance.get("audit_version") if isinstance(provenance, dict) else None
    return (
        title, field, initial, label, str(record.get("mode") or ""),
        object_id, 1 if record.get("human_best") else 0,
        _numeric_score(record, "confidence"), _numeric_score(record, "usefulness"),
        _numeric_score(record, "engraving"), _numeric_score(record, "fullness"),
        _numeric_score(record, "size"), _numeric_score(record, "completeness"),
        _numeric_score(record, "isolation"), _numeric_score(record, "semantic_label"),
        _numeric_score(record, "semantic_field"), float(bool(record.get("engraved"))),
        provenance_state, provenance_reason, provenance_version,
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
        "provenance_state": "search_provenance_state",
    }
    for name, column in columns.items():
        value = filters.get(name)
        if value not in (None, "", ALL):
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

            silhouette = metadata.get("silhouette")
            silhouette_field = (
                silhouette.get("field") if isinstance(silhouette, dict) else None
            )

            record = {
                "label": label_dir.name,
                "field": silhouette_field or ALL,
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