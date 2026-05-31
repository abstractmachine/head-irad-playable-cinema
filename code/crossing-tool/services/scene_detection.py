"""Scene detection from shot embeddings and annotations.

Strategy:
  1. Compute cosine distance between consecutive shot embeddings (primary signal).
  2. Add lightweight annotation signals: setting change, character change, motif change.
  3. Apply an adaptive threshold (mean + scale * std) to find candidate boundaries.
  4. Enforce minimum scene length via non-maximum suppression.

The resulting Scene column is a first-pass estimate intended for manual review
inside the Shotlist Visualizer.

Weighting:
  boundary_score = 0.70 * embedding_distance
                 + 0.15 * setting_change
                 + 0.10 * character_change
                 + 0.05 * motif_change
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_EMBED_WEIGHT = 0.70
_SETTING_WEIGHT = 0.15
_CHARACTER_WEIGHT = 0.10
_MOTIF_WEIGHT = 0.05


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_annotations(project_path: str, filename: str, media_type: str) -> list[dict]:
    """Load annotation JSON for *filename*. Returns an empty list when absent."""
    stem = Path(filename).stem
    ann_path = (
        Path(project_path)
        / "data"
        / "annotations"
        / "shots"
        / media_type
        / f"{stem}.json"
    )
    if not ann_path.exists():
        return []
    try:
        data = json.loads(ann_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _ann_field(ann: dict, field: str) -> Any:
    """Return a normalised, hashable value for *field* from an annotation dict."""
    val = ann.get(field)
    if val is None:
        return None
    if isinstance(val, list):
        return tuple(sorted(str(v).lower() for v in val if v is not None))
    return str(val).lower().strip()


# ---------------------------------------------------------------------------
# Core score computation
# ---------------------------------------------------------------------------

def compute_boundary_scores(
    embeddings: "np.ndarray",
    annotations: list[dict],
    ignore_flags: list[bool] | None = None,
) -> list[float]:
    """Compute a boundary confidence score for each adjacent shot pair.

    Returns a list of length N-1 where N = len(embeddings).
    Values are in [0, ~1] (can slightly exceed 1.0 in practice).

    Args:
        embeddings:   (N, D) float array of shot embeddings.
        annotations:  Raw annotation JSON list aligned to the embedding rows.
        ignore_flags: Per-shot boolean list; True → the shot is marked Ignore=Yes.
                      Boundaries adjacent to ignored shots are down-weighted.
    """
    import numpy as np

    n = len(embeddings)
    if n < 2:
        return []

    # Cosine distance between adjacent shots  (1 - cosine_similarity)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms < 1e-9, 1.0, norms)
    normed = embeddings / norms
    dot_products = np.einsum("ij,ij->i", normed[:-1], normed[1:])
    embedding_distances = 1.0 - np.clip(dot_products, -1.0, 1.0)  # (N-1,)

    # Build flat annotation list aligned to embeddings
    ann_list: list[dict] = []
    for entry in annotations:
        if isinstance(entry, dict):
            shot = entry.get("shot", {})
            ann_list.append(shot.get("annotation", {}) if isinstance(shot, dict) else {})
        else:
            ann_list.append({})
    # Pad to length n if annotations are sparse
    while len(ann_list) < n:
        ann_list.append({})

    setting_changes: list[float] = []
    character_changes: list[float] = []
    motif_changes: list[float] = []

    for i in range(n - 1):
        a0 = ann_list[i]
        a1 = ann_list[i + 1]

        # Setting: binary change
        s0 = _ann_field(a0, "setting")
        s1 = _ann_field(a1, "setting")
        setting_ch = 0.0 if (s0 is None or s1 is None or s0 == s1) else 1.0

        # Characters: Jaccard distance on the humans list
        h0 = set(_ann_field(a0, "humans") or ())
        h1 = set(_ann_field(a1, "humans") or ())
        union = h0 | h1
        char_ch = 0.0 if not union else 1.0 - len(h0 & h1) / len(union)

        # Motif: binary change
        m0 = _ann_field(a0, "motif")
        m1 = _ann_field(a1, "motif")
        motif_ch = 0.0 if (m0 is None or m1 is None or m0 == m1) else 1.0

        setting_changes.append(setting_ch)
        character_changes.append(char_ch)
        motif_changes.append(motif_ch)

    setting_arr = np.array(setting_changes, dtype=np.float32)
    character_arr = np.array(character_changes, dtype=np.float32)
    motif_arr = np.array(motif_changes, dtype=np.float32)

    scores: np.ndarray = (
        _EMBED_WEIGHT * embedding_distances
        + _SETTING_WEIGHT * setting_arr
        + _CHARACTER_WEIGHT * character_arr
        + _MOTIF_WEIGHT * motif_arr
    )

    # Reduce influence of ignored shots on adjacent boundaries
    if ignore_flags:
        flags = list(ignore_flags) + [False]  # pad to length n
        for i in range(len(scores)):
            if flags[i] or flags[i + 1]:
                scores[i] *= 0.5

    return scores.tolist()


# ---------------------------------------------------------------------------
# Boundary detection
# ---------------------------------------------------------------------------

def _detect_boundaries(
    scores: list[float],
    n_shots: int,
    min_scene_length: int = 3,
    scale: float = 1.0,
) -> list[int]:
    """Convert boundary scores to sorted 0-based shot indices where new scenes begin.

    A boundary at position k means shot k starts a new scene.
    Position 0 is always included (start of scene 0).

    The threshold is adaptive: mean + scale * std of all scores.
    Non-maximum suppression enforces *min_scene_length* between kept boundaries.
    """
    import numpy as np

    if not scores:
        return [0]

    arr = np.array(scores, dtype=np.float64)
    threshold = float(arr.mean() + scale * arr.std())

    # Gather positions above threshold (each position i → boundary before shot i+1)
    candidates: list[tuple[int, float]] = [
        (i + 1, float(arr[i]))
        for i in range(len(arr))
        if float(arr[i]) >= threshold
    ]

    if not candidates:
        return [0]

    # Non-maximum suppression: greedily keep highest-scoring boundaries,
    # discarding any that would create a segment shorter than min_scene_length.
    candidates_by_score = sorted(candidates, key=lambda x: -x[1])
    kept: list[int] = []

    for pos, _score in candidates_by_score:
        # Ensure at least min_scene_length shots on both sides
        if pos < min_scene_length:
            continue
        if n_shots - pos < min_scene_length:
            continue
        if all(abs(pos - k) >= min_scene_length for k in kept):
            kept.append(pos)

    return [0] + sorted(kept)


# ---------------------------------------------------------------------------
# Scene assignment
# ---------------------------------------------------------------------------

def apply_scene_numbers(
    shots: list[dict],
    boundaries: list[int],
) -> list[dict]:
    """Return a new shot list with the Scene field set from *boundaries*.

    Scene 0 = shots before the first non-zero boundary.
    """
    result: list[dict] = []
    scene_num = 0
    boundary_set = set(b for b in boundaries if b > 0)

    for i, shot in enumerate(shots):
        if i in boundary_set:
            scene_num += 1
        updated = dict(shot)
        updated["Scene"] = str(scene_num)
        result.append(updated)

    return result


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def detect_scenes_for_movie(
    project_path: str,
    filename: str,
    media_type: str = "movies",
    *,
    min_scene_length: int = 3,
    scale: float = 1.0,
    force: bool = False,
) -> dict:
    """Detect scenes for a single movie.

    Returns a result dict with keys:
      - filename:           str
      - skipped:            bool
      - reason:             str | None (why skipped)
      - shots:              int
      - scenes:             int
      - boundaries:         int  (count of non-zero boundary positions)
      - boundary_positions: list[int]
      - scene_assignments:  list[int]  (scene number per shot)
    """
    from data.shotlist import read_shotlist, get_shotlist_path
    from data.index import load_embeddings

    _base: dict = {
        "filename": filename,
        "skipped": False,
        "reason": None,
        "shots": 0,
        "scenes": 0,
        "boundaries": 0,
        "boundary_positions": [],
        "scene_assignments": [],
    }

    shotlist_path = get_shotlist_path(project_path, filename, media_type)
    if not shotlist_path.exists():
        return {**_base, "skipped": True, "reason": "no shotlist"}

    shots = read_shotlist(project_path, filename, media_type)
    if not shots:
        return {**_base, "skipped": True, "reason": "empty shotlist"}

    # Respect existing Scene values unless --force.
    # A single distinct value (e.g. all "0") means detection was never run;
    # only skip when 2+ distinct values are present.
    if not force:
        existing = [s.get("Scene", "").strip() for s in shots]
        distinct = set(v for v in existing if v)
        if len(distinct) >= 2:
            return {
                **_base,
                "skipped": True,
                "reason": "existing Scene values",
                "shots": len(shots),
                "scenes": len(distinct),
            }

    embeddings = load_embeddings(project_path, filename, media_type)
    if embeddings is None or len(embeddings) == 0:
        return {**_base, "skipped": True, "reason": "no embeddings", "shots": len(shots)}

    annotations = _load_annotations(project_path, filename, media_type)

    ignore_flags = [
        str(s.get("Ignore", "")).strip().lower() == "yes"
        for s in shots
    ]

    # Align lengths to the shortest of (shots, embeddings)
    n = min(len(shots), len(embeddings))
    emb = embeddings[:n]
    ig = ignore_flags[:n]
    ann = annotations[:n] if len(annotations) >= n else annotations

    scores = compute_boundary_scores(emb, ann, ig)
    boundaries = _detect_boundaries(scores, n, min_scene_length=min_scene_length, scale=scale)

    # Build per-shot scene assignment
    scene_num = 0
    boundary_set = set(b for b in boundaries if b > 0)
    scene_assignments: list[int] = []
    for i in range(n):
        if i in boundary_set:
            scene_num += 1
        scene_assignments.append(scene_num)

    n_boundaries = len(boundary_set)
    n_scenes = scene_num + 1

    return {
        **_base,
        "shots": n,
        "scenes": n_scenes,
        "boundaries": n_boundaries,
        "boundary_positions": sorted(boundary_set),
        "scene_assignments": scene_assignments,
    }


def detect_scenes_for_all_movies(
    project_path: str,
    media_type: str = "movies",
    *,
    min_scene_length: int = 3,
    scale: float = 1.0,
    force: bool = False,
) -> dict:
    """Run scene detection across all available shotlists.

    Returns:
      {
        "processed": int,
        "updated": int,
        "skipped": int,
        "failed": int,
        "results": list[dict],
      }
    """
    from data.shotlist import list_shotlists

    shotlists = list_shotlists(project_path, media_type)
    if not shotlists:
        return {"processed": 0, "updated": 0, "skipped": 0, "failed": 0, "results": []}

    results: list[dict] = []
    updated = 0
    skipped = 0
    failed = 0

    for sl in shotlists:
        filename = sl["filename"]
        try:
            result = detect_scenes_for_movie(
                project_path,
                filename,
                media_type,
                min_scene_length=min_scene_length,
                scale=scale,
                force=force,
            )
            results.append(result)
            if result["skipped"]:
                skipped += 1
            else:
                updated += 1
        except Exception as exc:
            results.append({
                "filename": filename,
                "skipped": False,
                "error": str(exc),
                "shots": 0,
                "scenes": 0,
                "boundaries": 0,
                "boundary_positions": [],
                "scene_assignments": [],
            })
            failed += 1

    return {
        "processed": len(shotlists),
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }
