"""Derived browse indexes for the Illustration visualizer.

Canonical silhouette and engraving sidecars remain the source of truth. These
compact indexes exist only to avoid reconstructing a browse catalog from many
small files on every visualizer launch.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from uuid import uuid4


INDEX_SCHEMA_VERSION = 2
SOURCES = ("silhouettes", "engravings")


def _index_dir(project_path: str | Path) -> Path:
    return Path(project_path) / "data" / "indexes" / "illustration"


def index_path(project_path: str | Path, source: str, media_type: str) -> Path:
    _validate_source(source)
    return _index_dir(project_path) / f"{media_type}-{source}.jsonl"


def _legacy_index_path(project_path: str | Path, source: str, media_type: str) -> Path:
    return _index_dir(project_path) / f"{media_type}-{source}.json"


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


def load_index(project_path: str | Path, source: str, media_type: str) -> dict:
    """Read one browse index and return ``ready``, ``missing``, ``stale``, or ``error``."""
    project = Path(project_path)
    path = index_path(project, source, media_type)
    if not path.exists():
        if _legacy_index_path(project, source, media_type).exists():
            return {"status": "stale", "items": []}
        return {"status": "missing", "items": []}

    try:
        items = []
        with path.open("r", encoding="utf-8") as handle:
            document = json.loads(handle.readline())
            if document.get("schema_version") != INDEX_SCHEMA_VERSION:
                return {"status": "stale", "items": []}
            if document.get("revision") != _read_revision(project, source, media_type):
                return {"status": "stale", "items": []}

            for index, line in enumerate(handle):
                if not line.strip():
                    continue
                record = json.loads(line)
                for key in ("path", "output_png", "raw_png"):
                    value = record.get(key)
                    if value:
                        record[key] = project / value
                items.append(record)
                if index % 256 == 255:
                    time.sleep(0)
        filter_cache = document.get("filter_cache") or {}
        if filter_cache:
            filter_cache["fields"] = set(filter_cache.get("fields", []))
        return {
            "status": "ready",
            "items": items,
            "generated_at": document.get("generated_at"),
            "filter_cache": filter_cache,
        }
    except Exception as exc:
        return {"status": "error", "items": [], "error": str(exc)}


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

    document = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "source": source,
        "media_type": media_type,
        "revision": revision,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filter_cache": _build_filter_cache(records),
    }
    from data.annotate import atomic_write_text

    path = index_path(project, source, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(document, ensure_ascii=False)]
    lines.extend(
        json.dumps(_serialize_record(project, record), ensure_ascii=False)
        for record in records
    )
    atomic_write_text(path, "\n".join(lines) + "\n")
    _legacy_index_path(project, source, media_type).unlink(missing_ok=True)
    return {"status": "ready", "count": len(records), "path": path}


def rebuild_all(project_path: str | Path, media_type: str) -> dict:
    """Rebuild both source indexes for one media type."""
    return {
        source: rebuild_index(project_path, source, media_type)
        for source in SOURCES
    }


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


def _build_filter_cache(records: list[dict]) -> dict:
    films: set[str] = set()
    fields: set[str] = set()
    letters: set[str] = set()
    counts: dict[str, int] = {}
    for record in records:
        stem = re.sub(
            r"\s*\{tmdb-\d+\}", "", str(record.get("filename_stem", ""))
        ).strip()
        if stem:
            films.add(stem)
        fields.add(record.get("field") or "--all")
        label = record.get("label", "")
        if label:
            initial = label[0].upper()
            letters.add(initial if initial.isalpha() else "#")
            counts[label] = counts.get(label, 0) + 1
    return {
        "films": sorted(films, key=str.casefold),
        "fields": sorted(fields, key=str.casefold),
        "letters": (["#"] if "#" in letters else [])
        + sorted(letters - {"#"}, key=str.casefold),
        "counts": counts,
    }


def _scan_silhouettes(project: Path, media_type: str) -> list[dict]:
    from services.silhouette_catalog import scan_catalog

    return [
        record for record in scan_catalog(str(project), media_type=media_type)
        if "error" not in record
    ]


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