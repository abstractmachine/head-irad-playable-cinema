"""Sync Visualizer — frame-match service layer.

Provides a UI-independent API for finding the closest-matching indexed
shot frames to an incoming live-video frame.

Public API::

    catalog  = load_frame_catalog(project_path, media_type,
                                  title=None, all_items=True)
    results  = match_frame_vector(vector, catalog, top=5)
    results  = match_rgb_frame(frame_rgb, project_path, media_type,
                               title=None, all_items=True, top=5)

Each result dict has the shape::

    {
        "rank":        1,
        "score":       0.842,
        "media_type":  "movie",
        "title":       "The Searchers",
        "filename":    "The Searchers (1956) {tmdb-1234}.mp4",
        "shot_id":     "tmdb_1234@f000123-f000456",
        "start_frame": 123,
        "end_frame":   456,
        "start_time":  "00:01:23.456",
        "end_time":    "00:02:34.567",
        "motif":       "riding",
        "description": "...",
        "best_frame":  {...},        # may be None
    }

Matching is cosine similarity via dot-product of L2-normalised CLIP vectors.
Only rows where ``.frames.valid.npy`` is True are considered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Catalog dataclass
# ---------------------------------------------------------------------------

@dataclass
class FrameCatalog:
    """In-memory frame-embedding catalog for one scope (media_type + title/all)."""

    media_type:  str                    # "movie" or "gameplay"
    scope_key:   str                    # human label, e.g. "all" or "The Searchers"
    # Parallel arrays — one row per indexed annotation shot
    vectors:     np.ndarray             # float32 (N, dim)
    valid_mask:  np.ndarray             # bool    (N,)
    # Metadata rows — parallel to vectors
    rows:        list[dict]             # list of dicts with shot metadata

    @property
    def size(self) -> int:
        return len(self.rows)


# ---------------------------------------------------------------------------
# Module-level catalog cache
# ---------------------------------------------------------------------------

# key: (project_path, media_type, title_or_None, all_items)
_catalog_cache: dict[tuple, FrameCatalog] = {}


def _cache_key(project_path: str, media_type: str,
               title: str | None, all_items: bool) -> tuple:
    return (str(project_path), media_type, title, all_items)


# ---------------------------------------------------------------------------
# Path helpers (duplicated locally to avoid circular imports)
# ---------------------------------------------------------------------------

def _shots_dir(project_path: str, media_type: str) -> Path:
    return Path(project_path) / "data" / "annotations" / "shots" / media_type


def _frames_npy(project_path: str, filename: str, media_type: str) -> Path:
    stem = Path(filename).stem
    return _shots_dir(project_path, media_type) / f"{stem}.frames.npy"


def _frames_valid(project_path: str, filename: str, media_type: str) -> Path:
    stem = Path(filename).stem
    return _shots_dir(project_path, media_type) / f"{stem}.frames.valid.npy"


def _annotations_json(project_path: str, filename: str, media_type: str) -> Path:
    stem = Path(filename).stem
    return _shots_dir(project_path, media_type) / f"{stem}.annotations.json"


# ---------------------------------------------------------------------------
# Enumerate indexed files
# ---------------------------------------------------------------------------

def list_indexed_filenames(project_path: str, media_type: str) -> list[str]:
    """Return filenames that have a ``.frames.npy`` index, sorted."""
    shots_dir = _shots_dir(project_path, media_type)
    if not shots_dir.exists():
        return []
    return sorted(
        p.stem + p.suffix.replace(".frames", "")
        # glob for the stem files and reconstruct a rough filename
        # Actually: glob *.frames.npy, strip .frames.npy to get stem, then
        # look for matching annotation JSON for the true filename.
        for p in []  # filled below
    )


def _find_indexed_stems(project_path: str, media_type: str) -> list[Path]:
    """Return all .frames.npy paths under the shots directory."""
    shots_dir = _shots_dir(project_path, media_type)
    if not shots_dir.exists():
        return []
    return sorted(shots_dir.glob("*.frames.npy"))


# ---------------------------------------------------------------------------
# Single-file catalog loader
# ---------------------------------------------------------------------------

def _load_single_file_catalog(
    project_path: str,
    frames_npy_path: Path,
    media_type: str,
) -> tuple[np.ndarray, np.ndarray, list[dict], str]:
    """Load vectors, valid mask, and metadata for one .frames.npy file.

    Returns (vectors, valid_mask, rows, filename_stem).
    """
    stem = frames_npy_path.name[: -len(".frames.npy")]   # strip suffix

    # Find the annotation JSON — try *.annotations.json matching the stem
    ann_path = frames_npy_path.parent / f"{stem}.annotations.json"
    valid_path = frames_npy_path.parent / f"{stem}.frames.valid.npy"

    vectors = np.load(str(frames_npy_path)).astype("float32")

    if valid_path.exists():
        valid_mask = np.load(str(valid_path)).astype(bool)
    else:
        valid_mask = np.ones(len(vectors), dtype=bool)

    # Truncate to minimum length in case of mismatch
    n = min(len(vectors), len(valid_mask))
    vectors   = vectors[:n]
    valid_mask = valid_mask[:n]

    # Reconstruct a filename from the stem (best effort; real filename may
    # differ, but we store the stem which is deterministic)
    filename_guess = stem  # e.g. "The Searchers (1956) {tmdb-1234}"

    rows: list[dict] = []
    if ann_path.exists():
        try:
            with ann_path.open("r", encoding="utf-8") as fh:
                annotations = json.load(fh)
        except Exception:
            annotations = []
        for item in annotations:
            rows.append(_extract_row_meta(item, media_type, filename_guess))
    else:
        # No annotation JSON — emit placeholder rows
        rows = [
            {"media_type": media_type, "filename": filename_guess,
             "shot_id": str(i), "title": filename_guess,
             "start_frame": None, "end_frame": None,
             "start_time": None, "end_time": None,
             "motif": None, "description": None, "best_frame": None}
            for i in range(n)
        ]

    # Pad/trim rows to match n
    while len(rows) < n:
        rows.append(_empty_row(media_type, filename_guess))
    rows = rows[:n]

    return vectors, valid_mask, rows, filename_guess


def _extract_row_meta(item: dict, media_type: str, filename: str) -> dict:
    """Pull the fields we need from one annotation JSON item."""
    shot = item.get("shot", {}) if "shot" in item else item
    # Support both flat and nested annotation formats
    shot_id     = item.get("shot_id") or shot.get("shot_id") or item.get("id", "")
    start_frame = item.get("start_frame") or shot.get("start_frame")
    end_frame   = item.get("end_frame") or shot.get("end_frame")
    start_time  = item.get("start_time") or shot.get("start_time")
    end_time    = item.get("end_time") or shot.get("end_time")
    motif       = item.get("motif") or item.get("annotation", {}).get("motif")
    description = (item.get("description")
                   or item.get("annotation", {}).get("description")
                   or "")
    best_frame  = item.get("best_frame")
    # Title: derive from filename stem (strip year + tmdb suffix)
    title = _title_from_filename(filename)
    return {
        "media_type":  media_type,
        "filename":    filename,
        "title":       title,
        "shot_id":     shot_id,
        "start_frame": start_frame,
        "end_frame":   end_frame,
        "start_time":  start_time,
        "end_time":    end_time,
        "motif":       motif,
        "description": description,
        "best_frame":  best_frame,
    }


def _empty_row(media_type: str, filename: str) -> dict:
    return {
        "media_type": media_type, "filename": filename,
        "title": _title_from_filename(filename),
        "shot_id": "", "start_frame": None, "end_frame": None,
        "start_time": None, "end_time": None,
        "motif": None, "description": None, "best_frame": None,
    }


def _title_from_filename(filename: str) -> str:
    """Extract a human-readable title from a media filename stem.

    'The Searchers (1956) {tmdb-1234}' → 'The Searchers'
    'rdr2' → 'rdr2'
    """
    import re
    stem = Path(filename).stem
    # Strip trailing year and/or tmdb ID
    stem = re.sub(r"\s*\{[^}]*\}\s*$", "", stem)
    stem = re.sub(r"\s*\(\d{4}\)\s*$", "", stem)
    return stem.strip() or filename


# ---------------------------------------------------------------------------
# Public: load_frame_catalog
# ---------------------------------------------------------------------------

def load_frame_catalog(
    project_path: str,
    media_type:   str,
    *,
    title:     str | None = None,
    all_items: bool = False,
    force_reload: bool = False,
) -> FrameCatalog:
    """Load (or return cached) a frame catalog for the given scope.

    Args:
        project_path: Root project directory.
        media_type:   ``"movie"`` or ``"gameplay"``.
        title:        Restrict to one film/game by title substring (case-
                      insensitive match against the filename stem).
        all_items:    Load all indexed items for the media type.
        force_reload: Bypass the module-level cache.

    At least one of *title* or *all_items* must be truthy.

    Returns:
        A :class:`FrameCatalog` with all qualifying rows loaded.
    """
    if not title and not all_items:
        raise ValueError("Specify title= or all_items=True")

    media_type = _canonical_media_type(media_type)
    key = _cache_key(project_path, media_type, title, all_items)
    if not force_reload and key in _catalog_cache:
        return _catalog_cache[key]

    npy_paths = _find_indexed_stems(project_path, media_type)
    if not npy_paths:
        # Return empty catalog
        catalog = FrameCatalog(
            media_type=media_type,
            scope_key=title or "all",
            vectors=np.zeros((0, 512), dtype="float32"),
            valid_mask=np.zeros(0, dtype=bool),
            rows=[],
        )
        _catalog_cache[key] = catalog
        return catalog

    selected = npy_paths
    if title:
        tl = title.lower()
        selected = [p for p in npy_paths
                    if tl in p.name.lower()]

    all_vectors:    list[np.ndarray] = []
    all_valid:      list[np.ndarray] = []
    all_rows:       list[dict] = []

    for npy_path in selected:
        try:
            vecs, valid, rows, _ = _load_single_file_catalog(
                project_path, npy_path, media_type)
        except Exception:
            continue
        all_vectors.append(vecs)
        all_valid.append(valid)
        all_rows.extend(rows)

    if all_vectors:
        vectors   = np.concatenate(all_vectors, axis=0)
        valid_mask = np.concatenate(all_valid,  axis=0)
    else:
        vectors   = np.zeros((0, 512), dtype="float32")
        valid_mask = np.zeros(0, dtype=bool)

    catalog = FrameCatalog(
        media_type=media_type,
        scope_key=title or "all",
        vectors=vectors,
        valid_mask=valid_mask,
        rows=all_rows,
    )
    _catalog_cache[key] = catalog
    return catalog


# ---------------------------------------------------------------------------
# Public: match_frame_vector
# ---------------------------------------------------------------------------

def match_frame_vector(
    vector:  "np.ndarray",
    catalog: FrameCatalog,
    *,
    top: int = 5,
) -> list[dict]:
    """Find the top-k closest catalog shots to *vector* by cosine similarity.

    Args:
        vector:  1-D float32 array of shape ``(dim,)`` — should be L2-normalised.
        catalog: Loaded :class:`FrameCatalog`.
        top:     Number of results to return.

    Returns:
        List of result dicts sorted by descending score, length ≤ *top*.
    """
    if catalog.size == 0:
        return []

    # Work only on valid rows
    valid_idx = np.where(catalog.valid_mask)[0]
    if len(valid_idx) == 0:
        return []

    valid_vecs = catalog.vectors[valid_idx]   # (M, dim)

    # Ensure query is normalised
    norm = np.linalg.norm(vector)
    if norm > 0:
        q = vector.astype("float32") / norm
    else:
        q = vector.astype("float32")

    scores = valid_vecs @ q   # (M,) cosine similarities

    k = min(top, len(scores))
    top_local = np.argpartition(scores, -k)[-k:]
    top_local = top_local[np.argsort(scores[top_local])[::-1]]

    results = []
    for rank, local_i in enumerate(top_local, start=1):
        global_i = int(valid_idx[local_i])
        row = dict(catalog.rows[global_i])
        row["rank"]  = rank
        row["score"] = float(scores[local_i])
        results.append(row)
    return results


# ---------------------------------------------------------------------------
# Public: match_rgb_frame
# ---------------------------------------------------------------------------

def match_rgb_frame(
    frame_rgb:    "np.ndarray",
    project_path: str,
    media_type:   str,
    *,
    title:     str | None = None,
    all_items: bool = False,
    top:       int = 5,
) -> list[dict]:
    """Embed *frame_rgb* with CLIP then search the catalog.

    Convenience wrapper that loads model + catalog and returns results.
    """
    from services.frame_vector import load_frame_vector_model, embed_rgb_frame

    media_type = _canonical_media_type(media_type)
    bundle  = load_frame_vector_model(project_path)
    vector  = embed_rgb_frame(frame_rgb, bundle)
    catalog = load_frame_catalog(project_path, media_type,
                                 title=title, all_items=all_items)
    return match_frame_vector(vector, catalog, top=top)


def match_image_path(
    image_path:   "str | Path",
    project_path: str,
    media_type:   str,
    *,
    title:     "str | None" = None,
    all_items: bool = False,
    top:       int = 5,
) -> list[dict]:
    """Load an image file, embed it with CLIP, then search the catalog.

    Convenience wrapper around :func:`match_rgb_frame` that accepts a file
    path instead of a pre-loaded numpy array.

    Args:
        image_path:   Path to a PIL-readable image (JPEG, PNG, …).
        project_path: Root project directory.
        media_type:   ``"movie"`` or ``"gameplay"``.
        title:        Restrict to one title (substring match).
        all_items:    Search all indexed items of the media type.
        top:          Number of results to return.

    Returns:
        Same list-of-dicts as :func:`match_frame_vector`.
    """
    from pathlib import Path
    from PIL import Image
    import numpy as _np

    img = Image.open(Path(image_path)).convert("RGB")
    frame_rgb = _np.array(img)
    return match_rgb_frame(frame_rgb, project_path, media_type,
                           title=title, all_items=all_items, top=top)


# ---------------------------------------------------------------------------
# Public: audit helper
# ---------------------------------------------------------------------------

def audit_catalog(project_path: str, media_type: str) -> list[dict]:
    """Return a list of status dicts for every .frames.npy in *media_type*.

    Each dict has keys: filename, vectors, valid_count, missing_annotation.
    """
    media_type = _canonical_media_type(media_type)
    npy_paths  = _find_indexed_stems(project_path, media_type)
    results = []
    for npy_path in npy_paths:
        stem = npy_path.name[: -len(".frames.npy")]
        ann_path   = npy_path.parent / f"{stem}.annotations.json"
        valid_path = npy_path.parent / f"{stem}.frames.valid.npy"
        try:
            vecs  = np.load(str(npy_path))
            n_vec = len(vecs)
        except Exception as exc:
            results.append({"filename": stem, "error": str(exc)})
            continue
        n_valid = 0
        if valid_path.exists():
            try:
                mask    = np.load(str(valid_path)).astype(bool)
                n_valid = int(mask[:n_vec].sum())
            except Exception:
                pass
        results.append({
            "filename":           stem,
            "vectors":            n_vec,
            "valid_count":        n_valid,
            "missing_annotation": not ann_path.exists(),
        })
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _canonical_media_type(mt: str) -> str:
    """Accept 'movies' as an alias; always return 'movie' or 'gameplay'."""
    mt = mt.lower().strip()
    if mt in ("movies", "movie"):
        return "movie"
    if mt in ("gameplay",):
        return "gameplay"
    raise ValueError(f"Unknown media_type {mt!r}; use 'movie' or 'gameplay'")
