"""Corpus-wide statistics for Crossing projects."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from data.metadata import load_json_metadata
from data.shotlist import read_shotlist
from data.subtitles import subtitle_path_for
from services.vocabulary_index import load_vocabulary_index, vocabulary_cache_is_stale


def _count_annotated_shots(project_path: str, media_type: str = "movie") -> int:
    ann_dir = Path(project_path) / "data" / "annotations" / "shots" / media_type
    if not ann_dir.exists():
        return 0

    total = 0
    for json_file in sorted(ann_dir.glob("*.json")):
        if json_file.name.endswith(".manifest.json"):
            continue
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, list):
            total += sum(1 for entry in payload if isinstance(entry, dict))
    return total


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
    if status.get("status") == "ready":
        facets = query_facets(project_path, "silhouettes", media_type)
        labels = Counter({entry["label"]: entry["count"] for entry in facets.get("labels", [])})
        return {"status": "ready", "count": int(status.get("count", 0)), "labels": labels}
    return {"status": status.get("status") or "error", "count": 0, "labels": Counter()}


# Worst-status-wins precedence when combining movie + gameplay: a missing
# index is treated as more severe than a stale one, which is more severe
# than a read error — any of the three means "not ready", never "ready".
_ILLUSTRATION_STATUS_PRIORITY = {"missing": 0, "stale": 1, "error": 2, "ready": 3}


def get_illustration_stats(project_path: Optional[str]) -> dict[str, Any]:
    """Return Illustrations-column data, sourced only from the Illustration
    visualizer's own browse index (data/indexes/illustration/).

    This deliberately never calls ``audit_catalog()`` or otherwise scans the
    silhouette catalog: if the index is missing/stale/errored for either
    media type, this reports an explicit unavailable/stale state instead of
    silently reconstructing anything. Cheap (small SQLite reads) — safe to
    call synchronously on every Project Visualizer open.

    Returns one of:
      ``{"state": "ready", "count": int, "labels": Counter[str]}``
      ``{"state": "unavailable", "reason": "illustration_index_missing"}``
      ``{"state": "stale", "reason": "illustration_index_stale"}``
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
    if worst["status"] == "ready":
        total = sum(s["count"] for s in per_media)
        labels: Counter[str] = Counter()
        for s in per_media:
            labels.update(s["labels"])
        return {"state": "ready", "count": total, "labels": labels}
    if worst["status"] == "missing":
        return {"state": "unavailable", "reason": "illustration_index_missing"}
    if worst["status"] == "stale":
        return {"state": "stale", "reason": "illustration_index_stale"}
    return {"state": "unavailable", "reason": "illustration_index_error"}


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
    """Count generated flipbook PDFs under output/flipbooks/.

    Mirrors generators/flipbook.py's documented output path convention
    (``<project>/output/flipbooks/<stem>-flipbook.pdf``).
    """
    base = Path(project_path) / "output" / "flipbooks"
    if not base.exists():
        return 0
    return sum(1 for f in base.glob("*-flipbook.pdf"))


def _count_annotated_shots_by_type(project_path: str) -> dict[str, int]:
    """Count annotation entries per media type."""
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
                payload = json.loads(json_file.read_text(encoding="utf-8"))
                if isinstance(payload, list):
                    total += sum(1 for e in payload if isinstance(e, dict))
            except (OSError, json.JSONDecodeError):
                continue
        result[mt_dir.name] = total
    return result


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

    annotated_shots_by_type = _count_annotated_shots_by_type(project_path)
    annotated_shots_movie = annotated_shots_by_type.get("movie", 0)
    annotated_shots_gameplay = annotated_shots_by_type.get("gameplay", 0)
    annotated_shots = annotated_shots_movie + annotated_shots_gameplay

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
        # Flipbooks (generated PDFs under output/flipbooks/)
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
                            vocabulary index). ``count`` is ``None`` — a
                            stale number is never shown as if it were current.
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
    anything) the column's DATAVIS region should draw, e.g.
    ``{"kind": "empty"}``. V0 intentionally implements no real per-column
    visualization yet (see ``get_project_columns`` docstring) but keeps this
    shape so later versions can add per-column visualization strategies
    without changing the column model itself.
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

CORPUS_STATS_SCHEMA_VERSION = 2


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


# Columns computed live (see get_live_project_columns) — (id, title). Cheap
# enough to compute on every call without a persisted cache: Movies/Gameplay/
# Shots read project metadata/annotation files directly; Illustrations reads
# only the illustration index (see get_illustration_stats). None of the four
# ever touches the silhouette catalog or calls audit_catalog().
_LIVE_COLUMN_TITLES: tuple[tuple[str, str], ...] = (
    ("movies", "Movies"),
    ("gameplay", "Gameplay"),
    ("shots", "Shots"),
    ("illustrations", "Illustrations"),
)

# Columns sourced only from the persisted corpus-stats cache — never
# recomputed here, per the "GUI opening -> READ CACHED STATS, never
# RECALCULATE STATS" rule. (id, title, corpus_stats key)
_CACHED_COLUMN_SPECS: tuple[tuple[str, str, str], ...] = (
    ("vocabulary", "Vocabulary", "vocabulary_terms"),
    ("segments", "Segments", "detected_scenes"),
    ("flipbooks", "Flipbooks", "flipbooks"),
)

# The fixed (id, title) pairs for all seven V0 columns, in the display order
# the GUI grid always uses — independent of which tier each column is
# sourced from internally, so a GUI can render headers/placeholders
# immediately, before any column value has been computed or read.
PROJECT_COLUMN_IDS_AND_TITLES: tuple[tuple[str, str], ...] = (
    ("movies", "Movies"),
    ("gameplay", "Gameplay"),
    ("shots", "Shots"),
    ("vocabulary", "Vocabulary"),
    ("segments", "Segments"),
    ("flipbooks", "Flipbooks"),
    ("illustrations", "Illustrations"),
)


def _make_column(
    col_id: str, title: str, count: Optional[int],
    *, state: Optional[str] = None, reason: Optional[str] = None,
) -> ProjectColumn:
    if state is None:
        state = "ready" if count is not None else "unavailable"
    return ProjectColumn(
        id=col_id, title=title, count=count, datavis={"kind": "empty"},
        state=state, reason=reason,
    )


def get_live_project_columns(project_path: Optional[str]) -> list[ProjectColumn]:
    """Return the Movies/Gameplay/Shots/Illustrations columns, computed live.

    Cheap (proportional to movie/annotation count, plus small illustration-
    index reads) — never touches the silhouette catalog or calls
    audit_catalog(). Safe to call synchronously, but Project Visualizer
    still calls it off the GUI thread alongside `get_cached_project_columns`
    so each tier can be displayed as soon as it's ready.
    """
    movie_count: Optional[int] = None
    gameplay_count: Optional[int] = None
    shots_count: Optional[int] = None

    if project_path:
        try:
            movie_count = len(load_json_metadata(project_path, "movie"))
        except (OSError, json.JSONDecodeError):
            pass
        try:
            gameplay_count = len(load_json_metadata(project_path, "gameplay"))
        except (OSError, json.JSONDecodeError):
            pass
        try:
            annotated_shots_by_type = _count_annotated_shots_by_type(project_path)
            shots_count = sum(annotated_shots_by_type.values())
        except OSError:
            pass

    illustration_stats = get_illustration_stats(project_path)
    if illustration_stats["state"] == "ready":
        illustrations_column = _make_column(
            "illustrations", "Illustrations", illustration_stats["count"],
        )
    else:
        illustrations_column = _make_column(
            "illustrations", "Illustrations", None,
            state=illustration_stats["state"], reason=illustration_stats.get("reason"),
        )

    return [
        _make_column("movies", "Movies", movie_count),
        _make_column("gameplay", "Gameplay", gameplay_count),
        _make_column("shots", "Shots", shots_count),
        illustrations_column,
    ]


def get_cached_project_columns(project_path: Optional[str]) -> list[ProjectColumn]:
    """Return the Vocabulary/Segments/Flipbooks columns.

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
    return [
        _make_column(col_id, title, stats.get(stat_key))
        for col_id, title, stat_key in _CACHED_COLUMN_SPECS
    ]


def get_project_columns(project_path: Optional[str]) -> list[ProjectColumn]:
    """Return the fixed V0 set of all seven Project Visualizer columns.

    Always fast and synchronous — never performs a full corpus traversal.
    See `get_live_project_columns` and `get_cached_project_columns` for the
    two tiers this reads (kept separately callable so a GUI worker can
    display each tier as soon as it's ready rather than waiting for both).
    Always returned in `PROJECT_COLUMN_IDS_AND_TITLES`'s fixed display
    order, regardless of which tier each column came from.

    Every column's ``datavis`` is ``{"kind": "empty"}`` in V0: this is a
    structural prototype, not a data-visualization implementation, and every
    'obvious' per-column visualization (proportional vocabulary sizing,
    palette swatches, embedding/density maps, segmentation maps) is
    explicitly out of scope for V0.
    """
    by_id = {
        column.id: column
        for column in get_live_project_columns(project_path) + get_cached_project_columns(project_path)
    }
    return [by_id[col_id] for col_id, _title in PROJECT_COLUMN_IDS_AND_TITLES]