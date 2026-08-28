"""Corpus-wide statistics for Crossing projects."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from data.annotate import UNTYPED_SHOT_TYPE, canonical_shot_type
from data.metadata import load_json_metadata
from data.shotlist import read_shotlist
from data.subtitles import subtitle_path_for
from services.vocabulary_index import load_vocabulary_index, vocabulary_cache_is_stale


def _count_detected_scenes(project_path: str, metadata: list[dict]) -> int:
    total = 0
    for entry in metadata:
        filename = entry.get("filename")
        if not filename:
            continue
        try:
            shots = read_shotlist(project_path, filename, "movie")
        except FileNotFoundError:
            continue

        scenes = {
            str(row.get("Scene", "")).strip()
            for row in shots
            if str(row.get("Scene", "")).strip()
        }
        total += len(scenes)
    return total


def _count_present_assets(project_path: str, metadata: list[dict], exists_fn) -> int:
    count = 0
    for entry in metadata:
        filename = entry.get("filename")
        if filename and exists_fn(project_path, str(filename)):
            count += 1
    return count


def _shotlist_path_exists(project_path: str, filename: str) -> bool:
    from data.shotlist import get_shotlist_path

    return get_shotlist_path(project_path, filename, "movie").exists()


def _subtitle_path_exists(project_path: str, filename: str) -> bool:
    return subtitle_path_for(project_path, "movie", filename) is not None


def _illustration_stats_for_media_type(project_path: str, media_type: str) -> dict[str, Any]:
    """Raw illustration-index status for one media type.

    Sourced only from the Illustration visualizer's own browse index (a
    small SQLite file under data/indexes/illustration/) — never scans the
    silhouette catalog itself. Returns
    ``{"status": "ready"|"missing"|"stale"|"error", "count": int, "labels": Counter}``.
    """
    from services.illustration_index import load_index, query_facets

    status = load_index(project_path, "silhouettes", media_type)
    if status.get("usable"):
        facets = query_facets(project_path, "silhouettes", media_type)
        labels = Counter({entry["label"]: entry["count"] for entry in facets.get("labels", [])})
        return {
            "status": status["status"],
            "count": int(status.get("count", 0)),
            "labels": labels,
            "usable": True,
        }
    return {
        "status": status.get("status") or "error",
        "count": 0,
        "labels": Counter(),
        "usable": False,
    }


# Worst-status-wins precedence when combining movie + gameplay: a missing
# index is treated as more severe than a stale one, which is more severe
# than a read error — any of the three means "not ready", never "ready".
_ILLUSTRATION_STATUS_PRIORITY = {"missing": 0, "stale": 1, "error": 2, "ready": 3}


def get_illustration_stats(project_path: Optional[str]) -> dict[str, Any]:
    """Return silhouette summary data from the Illustration browse index.

    This deliberately never calls ``audit_catalog()`` or otherwise scans the
    silhouette catalog: if the index is missing/stale/errored for either
    media type, this reports an explicit unavailable/stale state instead of
    silently reconstructing anything. Cheap (small SQLite reads) — safe to
    call synchronously from lightweight statistics consumers.

    Returns one of:
      ``{"state": "ready", "count": int, "labels": Counter[str]}``
    ``{"state": "stale", "count": int, "labels": Counter[str], ...}``
      ``{"state": "unavailable", "reason": "illustration_index_missing"}``
    ``{"state": "stale", "reason": "illustration_index_stale"}`` when
    the stale artifact is incompatible and cannot be queried
      ``{"state": "unavailable", "reason": "illustration_index_error"}``
      ``{"state": "unavailable", "reason": "no_project"}``
    """
    if not project_path:
        return {"state": "unavailable", "reason": "no_project"}

    per_media = [
        _illustration_stats_for_media_type(project_path, media_type)
        for media_type in ("movie", "gameplay")
    ]
    worst = min(per_media, key=lambda s: _ILLUSTRATION_STATUS_PRIORITY.get(s["status"], -1))
    if worst["status"] in ("ready", "stale") and all(
        result.get("usable") for result in per_media
    ):
        total = sum(s["count"] for s in per_media)
        labels: Counter[str] = Counter()
        for s in per_media:
            labels.update(s["labels"])
        result = {"state": worst["status"], "count": total, "labels": labels}
        if worst["status"] == "stale":
            result["reason"] = "illustration_index_stale"
        return result
    if worst["status"] == "missing":
        return {"state": "unavailable", "reason": "illustration_index_missing"}
    if worst["status"] == "stale":
        return {"state": "stale", "reason": "illustration_index_stale"}
    return {"state": "unavailable", "reason": "illustration_index_error"}


def get_indexed_field_stats(
    project_path: Optional[str], source: str,
) -> dict[str, Any]:
    """Merge one source's usable movie/gameplay field counts for Project.

    A same-schema stale index retains its last count and field distribution;
    incompatible stale artifacts carry only their stale state and reason.
    """
    reason_stems = {"silhouettes": "silhouette", "engravings": "engraving"}
    if source not in reason_stems:
        raise ValueError(f"Unknown Illustration index source: {source}")
    if not project_path:
        return {"state": "unavailable", "reason": "no_project"}

    from services.illustration_index import query_field_counts

    per_media = [
        query_field_counts(project_path, source, media_type)
        for media_type in ("movie", "gameplay")
    ]
    worst = min(
        per_media,
        key=lambda result: _ILLUSTRATION_STATUS_PRIORITY.get(
            result.get("status", "error"), -1,
        ),
    )
    status = worst.get("status") or "error"
    stale_is_usable = status == "stale" and all(
        result.get("usable") is not False for result in per_media
    )
    if status != "ready" and not stale_is_usable:
        state = "stale" if status == "stale" else "unavailable"
        return {
            "state": state,
            "reason": f"{reason_stems[source]}_index_{status}",
        }

    counts: Counter[tuple[str, bool]] = Counter()
    expected_total = 0
    for result in per_media:
        expected_total += int(result.get("count", 0))
        for item in result.get("fields", []):
            key = (str(item["field"]), item.get("synthetic") is True)
            counts[key] += int(item["count"])
    fields = []
    for (field, synthetic), count in sorted(
        counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1]),
    ):
        result = {"field": field, "count": count}
        if synthetic:
            result["synthetic"] = True
        fields.append(result)
    if sum(item["count"] for item in fields) != expected_total:
        return {
            "state": "unavailable",
            "reason": f"{reason_stems[source]}_index_error",
        }
    result = {"state": status, "count": expected_total, "fields": fields}
    if status == "stale":
        result["reason"] = f"{reason_stems[source]}_index_stale"
    return result


def get_top_silhouette_labels(project_path: str, limit: int = 10) -> list[tuple[str, int]]:
    """Return the most common silhouette labels across the corpus.

    Sourced only from the illustration index (see ``get_illustration_stats``)
    — returns an empty list, rather than scanning the catalog, when that
    index isn't ready for both media types.
    """
    stats = get_illustration_stats(project_path)
    if stats["state"] != "ready":
        return []
    return sorted(stats["labels"].items(), key=lambda item: (-item[1], item[0]))[:limit]


# ---------------------------------------------------------------------------
# New extended counters
# ---------------------------------------------------------------------------

def _count_best_frames(project_path: str) -> dict[str, int]:
    """Count PNG best-frame images under media/frames/best/<media_type>/."""
    result: dict[str, int] = {}
    base = Path(project_path) / "media" / "frames" / "best"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if mt_dir.is_dir():
            result[mt_dir.name] = sum(1 for _ in mt_dir.rglob("*.png"))
    return result


def _count_motifs(project_path: str) -> dict[str, int]:
    """Count shots with a canonical shot.motif string in annotation JSON, by media type."""
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "annotations" / "shots"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if not mt_dir.is_dir():
            continue
        total = 0
        for json_file in mt_dir.glob("*.json"):
            if json_file.name.endswith(".manifest.json"):
                continue
            try:
                entries = json.loads(json_file.read_text(encoding="utf-8"))
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    shot = entry.get("shot")
                    if not isinstance(shot, dict):
                        continue
                    motif = shot.get("motif")
                    if isinstance(motif, str) and motif.strip():
                        total += 1
            except (OSError, json.JSONDecodeError):
                continue
        result[mt_dir.name] = total
    return result


def _count_palettes(project_path: str) -> dict[str, int]:
    """Count palette JSON files that exist, broken down by media type."""
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "palettes"
    if not base.exists():
        return result
    # palettes live directly under data/palettes/ (one file per media item,
    # named after the video stem, not nested by media type).
    count = sum(1 for f in base.rglob("*.json"))
    if count:
        result["total"] = count
    return result


def _count_embeddings(project_path: str) -> dict[str, int]:
    """Count annotation-embedding ``.annotations.npy`` files under data/annotations/shots/.

    Counts only ``*.annotations.npy`` — the canonical new name.
    Old ambiguous ``*.npy`` files (pre-migration) are intentionally not counted
    here so that the stats reflect the new canonical naming convention.
    """
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "annotations" / "shots"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if mt_dir.is_dir():
            count = sum(1 for f in mt_dir.glob("*.annotations.npy"))
            result[mt_dir.name] = count
    return result


def _count_frame_embeddings(project_path: str) -> dict[str, int]:
    """Count frame-embedding .frames.npy files under data/annotations/shots/<media_type>/.

    Counts only ``*.frames.npy`` — does not count ``*.frames.valid.npy``.
    """
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "annotations" / "shots"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if mt_dir.is_dir():
            count = sum(
                1
                for f in mt_dir.glob("*.frames.npy")
                if not f.name.endswith(".frames.valid.npy")
            )
            result[mt_dir.name] = count
    return result


def _count_shots_with_best_frame(project_path: str) -> dict[str, int]:
    """Count annotation entries that have a best_frame.frame value, per media type."""
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "annotations" / "shots"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if not mt_dir.is_dir():
            continue
        total = 0
        for json_file in mt_dir.glob("*.json"):
            if json_file.name.endswith(".manifest.json"):
                continue
            try:
                entries = json.loads(json_file.read_text(encoding="utf-8"))
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    shot = entry.get("shot")
                    if not isinstance(shot, dict):
                        continue
                    bf = shot.get("best_frame")
                    if bf and isinstance(bf, dict) and bf.get("frame") is not None:
                        total += 1
            except (OSError, json.JSONDecodeError):
                continue
        result[mt_dir.name] = total
    return result


def _count_flipbooks(project_path: str) -> int:
    """Count generated flipbook PDFs under outputs/flipbooks/.

    Mirrors generators/flipbook.py's documented output path convention
    (``<project>/outputs/flipbooks/<stem>-flipbook.pdf``).
    """
    base = Path(project_path) / "outputs" / "flipbooks"
    if not base.exists():
        return 0
    return sum(1 for f in base.glob("*-flipbook.pdf"))


_ANNOTATION_MEDIA_TYPES = ("movie", "gameplay")
def _aggregate_annotated_shots(project_path: str) -> dict[str, Any]:
    """Aggregate the canonical annotated-shot population and its type values."""
    by_media_type = {media_type: 0 for media_type in _ANNOTATION_MEDIA_TYPES}
    type_counts: Counter[tuple[str, bool]] = Counter()
    base = Path(project_path) / "data" / "annotations" / "shots"
    for media_type in _ANNOTATION_MEDIA_TYPES:
        annotation_dir = base / media_type
        if not annotation_dir.is_dir():
            continue
        for json_file in sorted(annotation_dir.glob("*.json")):
            if json_file.name.endswith(".manifest.json"):
                continue
            try:
                payload = json.loads(json_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, list):
                continue
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                by_media_type[media_type] += 1
                shot = entry.get("shot")
                annotation = shot.get("annotation") if isinstance(shot, dict) else None
                type_value = canonical_shot_type(annotation)
                type_counts[(type_value, type_value == UNTYPED_SHOT_TYPE)] += 1

    types = [
        {"name": name, "count": count, "synthetic": synthetic}
        for (name, synthetic), count in sorted(
            type_counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
    ]
    return {
        "total": sum(by_media_type.values()),
        "by_media_type": by_media_type,
        "types": types,
    }


def get_corpus_stats(project_path: str) -> dict[str, Any]:
    """Return corpus-level project statistics.

    Scans annotation/shotlist/palette/embedding files directly (proportional
    to shot count — not the silhouette catalog), so this is not free, but it
    never calls ``audit_catalog()``: silhouette/illustration stats are read
    only from the illustration index (see ``get_illustration_stats``) and
    come back as an explicit unavailable/stale state, not a number, when
    that index isn't ready. Callers on a GUI thread should still prefer the
    cached artifact (``get_corpus_stats_state`` / ``refresh_corpus_stats_cache``)
    over calling this directly. See ``get_project_columns``.
    """
    movie_metadata = load_json_metadata(project_path, "movie")
    gameplay_metadata = load_json_metadata(project_path, "gameplay")

    vocabulary_index = load_vocabulary_index(project_path, "movie")
    vocabulary_terms = int(vocabulary_index.get("meta", {}).get("total_tokens", 0))

    annotated_shots_aggregation = _aggregate_annotated_shots(project_path)
    annotated_shots_by_media_type = annotated_shots_aggregation["by_media_type"]
    annotated_shots_movie = annotated_shots_by_media_type["movie"]
    annotated_shots_gameplay = annotated_shots_by_media_type["gameplay"]
    annotated_shots = annotated_shots_aggregation["total"]

    detected_scenes = _count_detected_scenes(project_path, movie_metadata)

    illustration_stats = get_illustration_stats(project_path)
    if illustration_stats["state"] == "ready":
        silhouette_objects: Optional[int] = illustration_stats["count"]
        silhouette_labels: Optional[int] = len(illustration_stats["labels"])
    else:
        silhouette_objects = None
        silhouette_labels = None

    subtitle_files = _count_present_assets(project_path, movie_metadata, _subtitle_path_exists)
    shotlists = _count_present_assets(project_path, movie_metadata, _shotlist_path_exists)

    best_frames_by_type = _count_best_frames(project_path)
    motifs_by_type = _count_motifs(project_path)
    palettes = _count_palettes(project_path)
    embeddings_by_type = _count_embeddings(project_path)
    frame_embeddings_by_type = _count_frame_embeddings(project_path)
    shots_with_best_frame_by_type = _count_shots_with_best_frame(project_path)

    flipbooks = _count_flipbooks(project_path)

    return {
        # Media
        "movie_videos": len(movie_metadata),
        "gameplay_videos": len(gameplay_metadata),
        # Annotations
        "annotated_shots": annotated_shots,
        "annotated_shots_movie": annotated_shots_movie,
        "annotated_shots_gameplay": annotated_shots_gameplay,
        "annotated_shot_types": annotated_shots_aggregation["types"],
        "detected_scenes": detected_scenes,
        # Best frames (CLIP-selected representative frames)
        "best_frames": sum(best_frames_by_type.values()),
        "best_frames_by_type": best_frames_by_type,
        "shots_with_best_frame": sum(shots_with_best_frame_by_type.values()),
        "shots_with_best_frame_by_type": shots_with_best_frame_by_type,
        # Motifs
        "motifs": sum(motifs_by_type.values()),
        "motifs_by_type": motifs_by_type,
        # Palettes
        "palettes": palettes.get("total", 0),
        # Annotation embeddings (.npy, excludes .frames.npy)
        "embeddings": sum(embeddings_by_type.values()),
        "embeddings_by_type": embeddings_by_type,
        # Frame embeddings (.frames.npy)
        "frame_embeddings": sum(frame_embeddings_by_type.values()),
        "frame_embeddings_by_type": frame_embeddings_by_type,
        # Vocabulary
        "vocabulary_terms": vocabulary_terms,
        # Silhouettes (sourced only from the illustration index — see
        # get_illustration_stats; None + a reason when that index isn't
        # ready for both media types, never a scanned/reconstructed number)
        "silhouette_objects": silhouette_objects,
        "silhouette_labels": silhouette_labels,
        "silhouette_state": illustration_stats["state"],
        "silhouette_reason": illustration_stats.get("reason"),
        # Assets
        "subtitle_files": subtitle_files,
        "shotlists": shotlists,
        # Flipbooks (generated PDFs under outputs/flipbooks/)
        "flipbooks": flipbooks,
    }


# ---------------------------------------------------------------------------
# Project Visualizer V0 — fixed column dashboard model
# ---------------------------------------------------------------------------

@dataclass
class ProjectColumn:
    """One column of the Project Visualizer's V0 structural dashboard.

    ``state`` is one of:
      ``"ready"``        — ``count`` holds a real, current value.
      ``"unavailable"``  — the artifact this column reads from has never
                            been built (e.g. no project set, no stats cache,
                            no illustration index yet). ``count`` is ``None``.
    ``"stale"``         — the artifact exists but is out of date relative
                    to the data it was built from (e.g. the
                    illustration index's revision no longer matches,
                    or the stats cache predates a since-changed
                    vocabulary index). A still-compatible index may
                    retain its last count/DATAVIS while the renderer
                    labels it stale; incompatible artifacts do not.
      ``"loading"``       — GUI-only placeholder meaning "not answered yet".
                            :func:`get_project_columns` never returns this —
                            it is synchronous and always returns a final
                            answer — it is constructed directly by the
                            renderer before its background worker resolves.

    ``reason`` is an optional short machine-readable code (e.g.
    ``"illustration_index_missing"``) explaining an ``"unavailable"``/
    ``"stale"`` state, for the renderer to map to display text. ``None`` for
    ``"ready"``/``"loading"`` columns, and for a bare/unspecified
    unavailable state.

    ``datavis`` is a small, renderer-agnostic dict describing what (if
    anything) the column's DATAVIS region should draw. Movies and Gameplay
    use ``{"kind": "media_items", "count": int, "items": [...]}``; Shots uses
    ``{"kind": "shot_types", "fields": [...]}``; Vocabulary uses
    ``{"kind": "vocabulary_fields", "fields": [...]}``; Silhouettes and
    Engravings use their corresponding ``*_fields`` payload; all other
    columns use ``{"kind": "empty"}``.
    """

    id: str
    title: str
    count: Optional[int]
    datavis: dict[str, Any]
    state: str = "ready"
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Cached corpus-stats artifact
#
# get_corpus_stats() is not free (proportional to shot/annotation count),
# and never calls audit_catalog(), so it must still not run on the GUI
# thread. The result is persisted to a small cache file (next to the
# Illustration visualizer's own derived indexes under data/indexes/) and
# only ever regenerated explicitly (via `crossing index stats --force`),
# never implicitly recomputed on read. Reading the cache (see
# `get_corpus_stats_state`) reports an explicit "missing"/"stale" state
# rather than silently rebuilding it.
# ---------------------------------------------------------------------------

CORPUS_STATS_SCHEMA_VERSION = 3


def corpus_stats_cache_path(project_path: str) -> Path:
    return Path(project_path) / "data" / "indexes" / "corpus_stats.json"


def load_cached_corpus_stats(project_path: str) -> Optional[dict[str, Any]]:
    """Return the persisted corpus-stats dict, or None if missing/unreadable/wrong schema.

    Cheap — only reads one small JSON file, never traverses project data.
    Does not check staleness — see `get_corpus_stats_state` for the full
    missing/stale/ready read used by the CLI and Project Visualizer.
    """
    path = corpus_stats_cache_path(project_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != CORPUS_STATS_SCHEMA_VERSION:
        return None
    stats = payload.get("stats")
    return stats if isinstance(stats, dict) else None


def _annotations_fingerprint(project_path: str) -> str:
    """Cheap mtime+size fingerprint over every annotation JSON file.

    Used only to detect whether the corpus-stats cache (whose figures are
    almost entirely derived from these files) has gone stale since it was
    generated — never reads file contents, so this stays fast even on large
    corpora (mirrors the same technique services/vocabulary_index.py already
    uses for its own cheap staleness check).
    """
    import hashlib

    digest = hashlib.md5()
    base = Path(project_path) / "data" / "annotations" / "shots"
    if not base.exists():
        return ""
    for mt_dir in sorted(base.iterdir()):
        if not mt_dir.is_dir():
            continue
        for json_file in sorted(mt_dir.glob("*.json")):
            if json_file.name.endswith(".manifest.json"):
                continue
            try:
                st = json_file.stat()
            except OSError:
                continue
            digest.update(f"{mt_dir.name}/{json_file.name}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    return digest.hexdigest()


def refresh_corpus_stats_cache(project_path: str) -> dict[str, Any]:
    """Recompute corpus stats and persist them, along with a staleness fingerprint.

    This is the only place `get_corpus_stats()` should be called from the
    GUI-adjacent code path — always off the GUI thread (see
    visualizers/project_visualizer.py) or from the CLI's explicit `--force`
    rebuild. Callers that only need to *read* the cache should use
    `get_corpus_stats_state` instead.
    """
    from data.annotate import atomic_write_text

    stats = get_corpus_stats(project_path)
    payload = {
        "schema_version": CORPUS_STATS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "annotations_fingerprint": _annotations_fingerprint(project_path),
        "stats": stats,
    }
    path = corpus_stats_cache_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
    return stats


def get_corpus_stats_state(project_path: Optional[str]) -> dict[str, Any]:
    """Return the corpus-stats cache's current read-only state.

    Never recomputes anything — the only work here is a small JSON read plus
    a cheap mtime/size fingerprint comparison (see `_annotations_fingerprint`).
    One of:

      ``{"state": "missing"}``                  — no valid cache exists yet
      ``{"state": "stale", "stats": {...}}``     — cache exists, but the
          annotation files it was computed from have changed since
      ``{"state": "ready", "stats": {...}}``     — cache exists and current
    """
    if not project_path:
        return {"state": "missing"}
    path = corpus_stats_cache_path(project_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "missing"}
    if payload.get("schema_version") != CORPUS_STATS_SCHEMA_VERSION:
        return {"state": "missing"}
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return {"state": "missing"}
    if payload.get("annotations_fingerprint") != _annotations_fingerprint(project_path):
        return {"state": "stale", "stats": stats}
    return {"state": "ready", "stats": stats}


# Columns computed live (see get_live_project_columns) — (id, title). Movies
# and Gameplay read metadata plus existing palette caches; the final two
# columns read only current Illustration indexes. None traverses the annotation
# corpus or silhouette/engraving catalogs.
_LIVE_COLUMN_TITLES: tuple[tuple[str, str], ...] = (
    ("movies", "Movies"),
    ("gameplay", "Gameplay"),
    ("silhouettes", "Silhouettes"),
    ("engravings", "Engravings"),
)

# Columns sourced only from the persisted corpus-stats cache — never
# recomputed here, per the "GUI opening -> READ CACHED STATS, never
# RECALCULATE STATS" rule. (id, title, corpus_stats key)
_CACHED_COLUMN_SPECS: tuple[tuple[str, str, str], ...] = (
    ("shots", "Shots", "annotated_shots"),
    ("vocabulary", "Vocabulary", "vocabulary_terms"),
)

# The fixed (id, title) pairs for all six V0 columns, in the display order
# the GUI grid always uses — independent of which tier each column is
# sourced from internally, so a GUI can render headers/placeholders
# immediately, before any column value has been computed or read.
PROJECT_COLUMN_IDS_AND_TITLES: tuple[tuple[str, str], ...] = (
    ("movies", "Movies"),
    ("gameplay", "Gameplay"),
    ("shots", "Shots"),
    ("vocabulary", "Vocabulary"),
    ("silhouettes", "Silhouettes"),
    ("engravings", "Engravings"),
)


def _make_column(
    col_id: str, title: str, count: Optional[int],
    *, state: Optional[str] = None, reason: Optional[str] = None,
    datavis: Optional[dict[str, Any]] = None,
) -> ProjectColumn:
    if state is None:
        state = "ready" if count is not None else "unavailable"
    return ProjectColumn(
        id=col_id, title=title, count=count, datavis=datavis or {"kind": "empty"},
        state=state, reason=reason,
    )


def _thumbnail_foreground_rgb(
    project_path: str,
    media_id: str,
    media_type: str,
) -> list[int] | None:
    if not project_path or not media_id:
        return None

    from data.palette import load_thumbnail_palette

    palette = load_thumbnail_palette(project_path, media_id, media_type)
    rgb = (palette or {}).get("thumbnail", {}).get("foreground", {}).get("rgb")
    if not (
        isinstance(rgb, list)
        and len(rgb) == 3
        and all(
            isinstance(channel, int)
            and not isinstance(channel, bool)
            and 0 <= channel <= 255
            for channel in rgb
        )
    ):
        return None
    return rgb


def _media_items_datavis(
    project_path: str,
    metadata: list[dict],
    media_type: str,
) -> dict[str, Any]:
    """Project DATAVIS identity projected from an already-loaded metadata list."""
    items = []
    for index, record in enumerate(metadata):
        filename = str(record.get("filename") or "")
        title = str(record.get("title") or Path(filename).stem or "(untitled)")
        year = record.get("year")
        media_id = str(record.get("media_id") or "")
        items.append({
            "index": index,
            "title": f"{title} ({year})" if year else title,
            "filename": filename,
            "media_type": media_type,
            "media_id": media_id,
            "thumbnail_foreground_rgb": (
                _thumbnail_foreground_rgb(project_path, media_id, media_type)
                if media_type == "gameplay" else None
            ),
        })
    return {"kind": "media_items", "count": len(items), "items": items}


def get_vocabulary_field_counts(
    project_path: str, expected_total: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Return unique vocabulary-term counts from largest to smallest."""
    index = load_vocabulary_index(project_path, "movie")
    fields = index.get("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("Vocabulary index has no valid fields mapping")

    canonical_order = index.get("meta", {}).get("vocabulary_fields", [])
    ordered_names = [name for name in canonical_order if name in fields]
    ordered_names.extend(name for name in fields if name not in ordered_names)
    result = [
        {"field": str(name), "count": len(fields[name])}
        for name in ordered_names
        if isinstance(fields[name], dict) and fields[name]
    ]
    result.sort(key=lambda item: item["count"], reverse=True)

    index_total = int(index.get("meta", {}).get("total_tokens", 0))
    field_total = sum(item["count"] for item in result)
    if field_total != index_total:
        raise ValueError(
            f"Vocabulary field total {field_total} does not match index total {index_total}"
        )
    if expected_total is not None and field_total != int(expected_total):
        raise ValueError(
            f"Vocabulary field total {field_total} does not match Project count {expected_total}"
        )
    return result


def _shot_types_datavis(stats: dict[str, Any]) -> dict[str, Any]:
    """Validate and project cached shot-type statistics for Project DATAVIS."""
    expected_total = stats.get("annotated_shots")
    fields = stats.get("annotated_shot_types")
    if (
        not isinstance(expected_total, int)
        or isinstance(expected_total, bool)
        or expected_total < 0
        or not isinstance(fields, list)
    ):
        raise ValueError("Invalid cached shot-type statistics")

    normalized = []
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("Invalid cached shot-type field")
        name = field.get("name")
        count = field.get("count")
        synthetic = field.get("synthetic")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            or not isinstance(synthetic, bool)
        ):
            raise ValueError("Invalid cached shot-type field")
        normalized.append({"name": name, "count": count, "synthetic": synthetic})

    if normalized != sorted(
        normalized,
        key=lambda item: (-item["count"], item["name"], item["synthetic"]),
    ):
        raise ValueError("Cached shot-type fields are not deterministically ordered")
    if sum(field["count"] for field in normalized) != expected_total:
        raise ValueError("Cached shot-type total does not match Project Shots count")
    return {"kind": "shot_types", "fields": normalized}


def get_live_project_columns(project_path: Optional[str]) -> list[ProjectColumn]:
    """Return the index-backed Project columns computed live.

    Cheap (proportional to media count, plus small Illustration-index reads)
    — never traverses shot annotations or canonical silhouette/engraving
    catalogs. Project Visualizer still calls it off the GUI thread so each
    tier can be displayed as soon as it is ready.
    """
    movie_metadata: Optional[list[dict]] = None
    gameplay_metadata: Optional[list[dict]] = None
    if project_path:
        try:
            movie_metadata = load_json_metadata(project_path, "movie")
        except (OSError, json.JSONDecodeError):
            pass
        try:
            gameplay_metadata = load_json_metadata(project_path, "gameplay")
        except (OSError, json.JSONDecodeError):
            pass
    indexed_field_stats = {
        source: get_indexed_field_stats(project_path, source)
        for source in ("silhouettes", "engravings")
    }

    def field_column(source: str, title: str) -> ProjectColumn:
        stats = indexed_field_stats[source]
        if stats["state"] not in ("ready", "stale") or "count" not in stats:
            return _make_column(
                source, title, None,
                state=stats["state"], reason=stats.get("reason"),
            )
        return _make_column(
            source, title, stats["count"],
            state=stats["state"], reason=stats.get("reason"),
            datavis={
                "kind": f"{source[:-1]}_fields",
                "fields": stats["fields"],
            },
        )

    return [
        _make_column(
            "movies", "Movies", len(movie_metadata) if movie_metadata is not None else None,
            datavis=(
                _media_items_datavis(project_path, movie_metadata, "movie")
                if movie_metadata is not None else None
            ),
        ),
        _make_column(
            "gameplay", "Gameplay",
            len(gameplay_metadata) if gameplay_metadata is not None else None,
            datavis=(
                _media_items_datavis(project_path, gameplay_metadata, "gameplay")
                if gameplay_metadata is not None else None
            ),
        ),
        field_column("silhouettes", "Silhouettes"),
        field_column("engravings", "Engravings"),
    ]


def get_cached_project_columns(project_path: Optional[str]) -> list[ProjectColumn]:
    """Return the Shots/Vocabulary columns.

    Sourced only from the persisted stats cache (see `get_corpus_stats_state`)
    — never recomputed here. Columns come back ``state="unavailable"`` when
    no cache exists yet, or ``state="stale"`` when the cache exists but the
    annotation data it was computed from has changed since — never silently
    triggering a rebuild in either case.
    """
    result = get_corpus_stats_state(project_path)
    state = result["state"]
    if state == "missing":
        return [
            _make_column(col_id, title, None, state="unavailable", reason="corpus_stats_missing")
            for col_id, title, _stat_key in _CACHED_COLUMN_SPECS
        ]
    if state == "stale":
        return [
            _make_column(col_id, title, None, state="stale", reason="corpus_stats_stale")
            for col_id, title, _stat_key in _CACHED_COLUMN_SPECS
        ]
    stats = result["stats"]
    columns = [
        _make_column(col_id, title, stats.get(stat_key))
        for col_id, title, stat_key in _CACHED_COLUMN_SPECS
    ]
    shots = columns[0]
    try:
        shots.datavis = _shot_types_datavis(stats)
    except ValueError:
        shots.count = None
        shots.state = "stale"
        shots.reason = "shot_type_stats_invalid"

    vocabulary = columns[1]
    if project_path and vocabulary.count is not None:
        try:
            fields = get_vocabulary_field_counts(project_path, vocabulary.count)
        except FileNotFoundError:
            vocabulary.count = None
            vocabulary.state = "unavailable"
            vocabulary.reason = "vocabulary_index_missing"
        except ValueError:
            vocabulary.count = None
            vocabulary.state = "stale"
            vocabulary.reason = "vocabulary_count_mismatch"
        else:
            if vocabulary_cache_is_stale(project_path, "movie"):
                vocabulary.count = None
                vocabulary.state = "stale"
                vocabulary.reason = "vocabulary_index_stale"
            else:
                vocabulary.datavis = {"kind": "vocabulary_fields", "fields": fields}
    return columns


def get_project_columns(project_path: Optional[str]) -> list[ProjectColumn]:
    """Return the fixed V0 set of all six Project Visualizer columns.

    Always fast and synchronous — never performs a full corpus traversal.
    See `get_live_project_columns` and `get_cached_project_columns` for the
    two tiers this reads (kept separately callable so a GUI worker can
    display each tier as soon as it's ready rather than waiting for both).
    Always returned in `PROJECT_COLUMN_IDS_AND_TITLES`'s fixed display
    order, regardless of which tier each column came from.

    Movies and Gameplay carry their metadata collection size as
    ``media_items``; Shots carries its ordered annotation-type distribution;
    Vocabulary, Silhouettes, and Engravings carry ordered field composition.
    """
    by_id = {
        column.id: column
        for column in get_live_project_columns(project_path) + get_cached_project_columns(project_path)
    }
    return [by_id[col_id] for col_id, _title in PROJECT_COLUMN_IDS_AND_TITLES]