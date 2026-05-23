"""Palette indexing: figure-ground colour extraction from best frames.

Cache path
----------
    <project>/data/palettes/<media_type>/<stem>.json

For each shot in a film the best-frame PNG (written by ``crossing annotate
frame``) is loaded and analysed using the figure-ground palette pipeline.

Algorithm — figure
------------------
1. Resize to 256×256 (LANCZOS) for texture detail.
2. Simplify texture via mean-shift filtering (``cv2.pyrMeanShiftFiltering``
   when OpenCV is available; skipped gracefully if absent).
3. Split into foreground / background regions:
   a. SAM2 semantic segmentation when a model is configured via
      ``crossing tool model set segmentation <name>``; objects that do not
      touch the frame edge and whose bounding-box centre lies within the
      inner 60 % of the frame are classified as foreground.
   b. Spatial border/center split when SAM2 is unavailable.
4. Cluster each region with agglomerative Ward clustering in CIELAB space
   (falls back to stratified k-means when scipy is absent).
5. Rank clusters by perceptual weight; apply a rescue pass when both
   dominant colours are near-black or insufficiently distinct.

Output schema
-------------
::

    "foreground": {
        "rgb":       [80, 92, 130],
        "lab":       [42.1, 5.3, -22.0],
        "luminance": 0.421,          # L* / 100
        "chroma":    0.229,          # sqrt(a*²+b*²) / 100
        "palette":   [               # top colours in this region
            {"rgb": [...], "lab": [...], "luminance": ..., "chroma": ...},
            ...
        ],
        "coverage":  0.35            # fraction of frame pixels in this region
    }

Output is fully deterministic when SAM2 is not in use.

Dependencies: Pillow (core dep), numpy (core dep).
Optional: cv2 (OpenCV) for mean-shift; scipy for Ward clustering;
          SAM2 for semantic figure-ground segmentation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from data.index import load_annotation_items
from data.media_id import build_shot_id, compute_media_id
from data.metadata import get_metadata
from data.shotlist import get_shotlist_path, read_shotlist

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Perceptual weighting tuning
_LAB_MIN_L         = 8.0   # L* below this is "near-black" and gets downweighted
_LAB_DARK_FRACTION = 0.70  # if this fraction of pixels is near-black, lower threshold
_N_CLUSTERS        = 5     # k-means cluster count (fallback when scipy absent)
_KMEANS_MAX_ITER   = 20    # max k-means iterations

# Low-key rescue pass thresholds
_NEAR_BLACK_L               = 15.0  # L* below this is "near-black"
_NEAR_BLACK_CHROMA          = 10.0  # C* below this (with low L*) = effectively black
_RESCUE_TRIGGER_MAX_L       = 20.0  # rescue triggers when both L* values are below this
_RESCUE_TRIGGER_MIN_DELTA_E = 15.0  # rescue triggers when pair CIE76 ΔE is below this

# Figure-ground pipeline constants
_FIG_RESIZE     = 256   # image resize for the figure pipeline
_FIG_MS_SP      = 10    # mean-shift spatial radius (pixels)
_FIG_MS_SR      = 20    # mean-shift color-range radius
_FIG_N_CLUSTERS = 8     # agglomerative clusters per region
_FIG_N_PALETTE  = 4     # palette entries kept per region
_FIG_MAX_SAMPLE = 1024  # max pixels sampled for Ward linkage (speed cap)


# ---------------------------------------------------------------------------
# Cache path helpers
# ---------------------------------------------------------------------------

def get_palette_path(project_path: str, filename: str, media_type: str) -> Path:
    """Return the canonical cache path for a palette JSON.

    ``<project>/data/palettes/<media_type>/<stem>.json``
    """
    stem = Path(filename).stem
    return Path(project_path) / "data" / "palettes" / media_type / f"{stem}.json"


def load_palette(project_path: str, filename: str, media_type: str) -> dict | None:
    """Load a cached palette JSON or return ``None`` if absent.

    Normalises legacy method names to ``"figure"`` so that caches created
    before the method rename remain readable without regeneration.
    """
    path = get_palette_path(project_path, filename, media_type)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("method") in {
            "figure_agglomerative", "lab_kmeans_weighted", "border_center_dominant",
        }:
            data["method"] = "figure"
        return data
    except Exception:
        return None


def save_palette(
    project_path: str,
    filename: str,
    media_type: str,
    data: dict,
    *,
    force: bool = False,
) -> Path:
    """Write *data* to the canonical palette cache path.

    Raises ``FileExistsError`` if the file already exists and *force* is False.
    """
    dest = get_palette_path(project_path, filename, media_type)
    if dest.exists() and not force:
        raise FileExistsError(
            f"Palette cache already exists: {dest}\n  Pass --force to overwrite."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest


def get_palette(project_path: str, filename: str, media_type: str) -> dict | None:
    """Return the cached palette for *filename*, or ``None`` if not found.

    This is a thin alias for ``load_palette`` intended for use from the CLI.
    """
    return load_palette(project_path, filename, media_type)


# ---------------------------------------------------------------------------
# LAB colour-science helpers (pure numpy — no extra dependencies)
# ---------------------------------------------------------------------------

def _rgb_to_lab(rgb_u8: np.ndarray) -> np.ndarray:
    """Convert ``(N, 3)`` uint8 sRGB to ``(N, 3)`` float32 CIELAB (D65).

    Uses the standard IEC 61966-2-1 sRGB primaries and the CIE 1976 L*a*b*
    formula.  Fully deterministic, no dependencies beyond numpy.
    """
    srgb = rgb_u8.astype(np.float32) / 255.0

    # sRGB → linear RGB (inverse gamma)
    linear = np.where(
        srgb <= 0.04045,
        srgb / 12.92,
        ((srgb + 0.055) / 1.055) ** 2.4,
    )

    # Linear RGB → CIE XYZ (D65 primaries, IEC 61966-2-1)
    M = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ],
        dtype=np.float32,
    )
    xyz = linear @ M.T  # (N, 3)

    # Normalise by D65 white point
    xyz = xyz / np.array([0.95047, 1.00000, 1.08883], dtype=np.float32)

    # XYZ → CIELAB (CIE 1976)
    eps   = np.float32(0.008856)
    kappa = np.float32(903.3)
    f = np.where(xyz > eps, np.cbrt(xyz), (kappa * xyz + 16.0) / 116.0)

    L = 116.0 * f[:, 1] - 16.0
    a = 500.0 * (f[:, 0] - f[:, 1])
    b = 200.0 * (f[:, 1] - f[:, 2])

    return np.stack([L, a, b], axis=1).astype(np.float32)


def _perceptual_weights(lab: np.ndarray) -> np.ndarray:
    """Compute per-pixel perceptual weights from CIELAB values ``(N, 3)``.

    Formula::

        weight = (lum_ramp + 0.1) × (1 + chroma_factor)

    ``lum_ramp``      — smooth ramp: 0 at the effective min-L threshold,
                        1 at L* = 40+.  Suppresses near-black pixels.
    ``chroma_factor`` — normalised chroma C* / 30, capped at 2.  Small
                        saturated highlights outweigh large dark walls.

    Dark-scene adaptation: when more than ``_LAB_DARK_FRACTION`` of pixels
    fall below ``_LAB_MIN_L``, the threshold is lowered to 2.0 so that
    moonlit blues, noir shadows, and dusk atmospheres are preserved.
    """
    L     = lab[:, 0]
    a     = lab[:, 1]
    b_ch  = lab[:, 2]
    chroma = np.sqrt(a * a + b_ch * b_ch)

    dark_frac  = float((L < _LAB_MIN_L).mean())
    eff_min_l  = 2.0 if dark_frac > _LAB_DARK_FRACTION else _LAB_MIN_L

    lum_ramp      = np.clip((L - eff_min_l) / max(40.0 - eff_min_l, 1.0), 0.0, 1.0)
    chroma_factor = np.clip(chroma / 30.0, 0.0, 2.0)

    return (lum_ramp + 0.1) * (1.0 + chroma_factor)


# ---------------------------------------------------------------------------
# Rescue pass helpers (pure numpy — no extra dependencies)
# ---------------------------------------------------------------------------

def _delta_e_cie76(
    lab1: "list | np.ndarray",
    lab2: "list | np.ndarray",
) -> float:
    """CIE 1976 ΔE between two LAB colour triplets."""
    a = np.asarray(lab1, dtype=np.float64)
    b = np.asarray(lab2, dtype=np.float64)
    d = a - b
    return float(np.sqrt((d * d).sum()))


def _is_near_black(lab: "list | np.ndarray") -> bool:
    """Return True if a LAB colour is perceptually near-black.

    Requires both low luminance (L* < ``_NEAR_BLACK_L``) *and* low chroma
    (C* < ``_NEAR_BLACK_CHROMA``).  A dark but chromatic colour (e.g. dark
    blue, deep crimson) is *not* near-black and will return False.
    """
    L_val  = float(lab[0])
    chroma = float(np.sqrt(float(lab[1]) ** 2 + float(lab[2]) ** 2))
    return L_val < _NEAR_BLACK_L and chroma < _NEAR_BLACK_CHROMA


def _pair_separation_score(
    fg_lab: "list | np.ndarray",
    bg_lab: "list | np.ndarray",
) -> float:
    """Perceptual separation score for a foreground/background pair.

    Returns |ΔL*| + |ΔC*| + ΔE(CIE76).  Higher means more perceptually
    distinct.  Used by the rescue pass to compare candidate pairs.
    """
    fg = np.asarray(fg_lab, dtype=np.float64)
    bg = np.asarray(bg_lab, dtype=np.float64)
    lum_delta    = abs(float(fg[0]) - float(bg[0]))
    fg_chroma    = float(np.sqrt(fg[1] ** 2 + fg[2] ** 2))
    bg_chroma    = float(np.sqrt(bg[1] ** 2 + bg[2] ** 2))
    chroma_delta = abs(fg_chroma - bg_chroma)
    delta_e      = _delta_e_cie76(fg, bg)
    return lum_delta + chroma_delta + delta_e


def _rank_region_candidates(
    pixels_rgb: np.ndarray,
    k: int = _N_CLUSTERS,
) -> "list[dict]":
    """Run LAB k-means and return all clusters ranked by perceptual weight.

    Each entry in the returned list contains:

    ``"rgb"``       — ``(R, G, B)`` int tuple
    ``"lab"``       — ``[L, a, b]`` float list (mean LAB of cluster)
    ``"luminance"`` — float (L*/100)
    ``"chroma"``    — float (C*/100)
    ``"weight"``    — float (total perceptual weight for the cluster)
    ``"size"``      — int (pixel count)

    Sorted highest to lowest perceptual weight.  Always returns at least one
    entry; falls back to a synthetic black entry on empty input.
    """
    if len(pixels_rgb) == 0:
        return [{
            "rgb":       (0, 0, 0),
            "lab":       [0.0, 0.0, 0.0],
            "luminance": 0.0,
            "chroma":    0.0,
            "weight":    0.0,
            "size":      0,
        }]

    lab     = _rgb_to_lab(pixels_rgb)
    weights = _perceptual_weights(lab)
    k       = min(k, len(pixels_rgb))
    _, labels = _kmeans(lab, k)

    candidates: "list[dict]" = []
    for j in range(k):
        mask  = labels == j
        count = int(mask.sum())
        if count == 0:
            continue
        cluster_w = float(weights[mask].sum())
        mean_rgb  = pixels_rgb[mask].astype(np.float64).mean(axis=0)
        mean_lab  = lab[mask].mean(axis=0)
        chroma    = float(np.sqrt(mean_lab[1] ** 2 + mean_lab[2] ** 2))
        candidates.append({
            "rgb": (
                int(round(mean_rgb[0])),
                int(round(mean_rgb[1])),
                int(round(mean_rgb[2])),
            ),
            "lab": [
                round(float(mean_lab[0]), 1),
                round(float(mean_lab[1]), 1),
                round(float(mean_lab[2]), 1),
            ],
            "luminance": round(float(mean_lab[0]) / 100.0, 3),
            "chroma":    round(chroma / 100.0, 3),
            "weight":    cluster_w,
            "size":      count,
        })

    candidates.sort(key=lambda c: c["weight"], reverse=True)
    return candidates


def _candidate_to_colour(cand: dict) -> dict:
    """Convert a candidate dict to a palette colour output dict.

    Strips internal ``"weight"`` and ``"size"`` fields; keeps the
    ``"rgb"``, ``"lab"``, ``"luminance"``, and ``"chroma"`` fields that
    are written to the palette JSON.
    """
    return {
        "rgb":       list(cand["rgb"]),
        "lab":       cand["lab"],
        "luminance": cand["luminance"],
        "chroma":    cand["chroma"],
    }


def _maybe_rescue_pair(
    fg_candidates: "list[dict]",
    bg_candidates: "list[dict]",
) -> "tuple[dict, dict, dict]":
    """Evaluate the fg/bg top pair; apply a low-key rescue pass if needed.

    Rescue is triggered when:

    - both best-candidate colours are individually near-black
      (``_is_near_black``), OR
    - both best L* values are below ``_RESCUE_TRIGGER_MAX_L`` AND the pair
      CIE76 ΔE is below ``_RESCUE_TRIGGER_MIN_DELTA_E``.

    When rescue is needed, all ``(fg_candidate × bg_candidate)`` pairs are
    scored with ``_pair_separation_score`` and the highest-scoring pair is
    chosen.  If no alternative improves on the original, the originals are
    returned unchanged (``rescue_applied = False``).

    Returns ``(foreground_colour, background_colour, diagnostics)``.

    Diagnostics keys:
    ``fg_bg_delta_e``, ``fg_bg_luminance_delta``, ``fg_bg_chroma_delta``,
    ``rescue_applied``, ``rescue_reason``, ``near_black_pair``,
    ``candidate_count_fg``, ``candidate_count_bg``.
    """
    fg_best = fg_candidates[0]
    bg_best = bg_candidates[0]

    fg_lab = fg_best["lab"]
    bg_lab = bg_best["lab"]

    pair_de      = _delta_e_cie76(fg_lab, bg_lab)
    lum_delta    = abs(float(fg_lab[0]) - float(bg_lab[0]))
    fg_chroma    = float(np.sqrt(float(fg_lab[1]) ** 2 + float(fg_lab[2]) ** 2))
    bg_chroma    = float(np.sqrt(float(bg_lab[1]) ** 2 + float(bg_lab[2]) ** 2))
    chroma_delta = abs(fg_chroma - bg_chroma)

    near_black_pair = _is_near_black(fg_lab) and _is_near_black(bg_lab)
    both_dark       = max(float(fg_lab[0]), float(bg_lab[0])) < _RESCUE_TRIGGER_MAX_L
    too_similar     = pair_de < _RESCUE_TRIGGER_MIN_DELTA_E
    rescue_needed   = near_black_pair or (both_dark and too_similar)

    diag: dict = {
        "fg_bg_delta_e":         round(pair_de, 2),
        "fg_bg_luminance_delta": round(lum_delta, 2),
        "fg_bg_chroma_delta":    round(chroma_delta, 2),
        "rescue_applied":        False,
        "rescue_reason":         None,
        "near_black_pair":       near_black_pair,
        "candidate_count_fg":    len(fg_candidates),
        "candidate_count_bg":    len(bg_candidates),
    }

    if not rescue_needed:
        return _candidate_to_colour(fg_best), _candidate_to_colour(bg_best), diag

    if near_black_pair and both_dark and too_similar:
        reason = "near_black_too_similar_and_dark"
    elif near_black_pair:
        reason = "near_black_pair"
    else:
        reason = "both_dark_and_too_similar"

    # Exhaustive search over all candidate pairs for best perceptual separation
    best_score   = _pair_separation_score(fg_lab, bg_lab)
    best_fc      = fg_best
    best_bc      = bg_best
    found_better = False

    for fc in fg_candidates:
        for bc in bg_candidates:
            score = _pair_separation_score(fc["lab"], bc["lab"])
            if score > best_score:
                best_score   = score
                best_fc      = fc
                best_bc      = bc
                found_better = True

    if not found_better:
        return _candidate_to_colour(fg_best), _candidate_to_colour(bg_best), diag

    # Recompute pair diagnostics for the rescued selection
    new_fg_lab    = best_fc["lab"]
    new_bg_lab    = best_bc["lab"]
    new_de        = _delta_e_cie76(new_fg_lab, new_bg_lab)
    new_lum_d     = abs(float(new_fg_lab[0]) - float(new_bg_lab[0]))
    new_fg_chroma = float(np.sqrt(float(new_fg_lab[1]) ** 2 + float(new_fg_lab[2]) ** 2))
    new_bg_chroma = float(np.sqrt(float(new_bg_lab[1]) ** 2 + float(new_bg_lab[2]) ** 2))
    new_chroma_d  = abs(new_fg_chroma - new_bg_chroma)

    diag.update({
        "rescue_applied":        True,
        "rescue_reason":         reason,
        "fg_bg_delta_e":         round(new_de, 2),
        "fg_bg_luminance_delta": round(new_lum_d, 2),
        "fg_bg_chroma_delta":    round(new_chroma_d, 2),
    })

    return _candidate_to_colour(best_fc), _candidate_to_colour(best_bc), diag


def _kmeans(
    lab: np.ndarray,
    k: int,
    max_iter: int = _KMEANS_MAX_ITER,
) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic k-means in LAB space.

    Initialises ``k`` centres by stratified sampling along the L* axis so
    that centres always span the luminance range.  No random seed needed.

    Returns ``(centers, labels)`` where *centers* is ``(k, 3)`` float32
    and *labels* is ``(N,)`` int32.
    """
    n = len(lab)
    k = min(k, n)
    if k == 0:
        return np.zeros((0, 3), dtype=np.float32), np.zeros(0, dtype=np.int32)

    # Stratified init: pick k pixels evenly spaced along sorted L*
    order     = np.argsort(lab[:, 0])
    positions = np.round(np.linspace(0, n - 1, k)).astype(int)
    centers   = lab[order[positions]].copy().astype(np.float64)
    pts       = lab.astype(np.float64)

    labels = np.zeros(n, dtype=np.int32)
    for _ in range(max_iter):
        # Vectorised squared-distance matrix  (n, k)
        diff   = pts[:, None, :] - centers[None, :, :]  # (n, k, 3)
        dists2 = (diff * diff).sum(axis=2)               # (n, k)
        new_labels = dists2.argmin(axis=1).astype(np.int32)

        if np.array_equal(new_labels, labels):
            break
        labels = new_labels

        for j in range(k):
            pts_j = pts[labels == j]
            if len(pts_j):
                centers[j] = pts_j.mean(axis=0)

    return centers.astype(np.float32), labels


# ---------------------------------------------------------------------------
# Colour extraction — two methods
# ---------------------------------------------------------------------------

# ===========================================================================
# Figure-ground colour analysis pipeline
# ===========================================================================
#
# Fallback ladder
# ---------------
# Level 1 (full):     SAM2 semantic segmentation + mean-shift simplification
#                     + agglomerative Ward clustering in CIELAB space.
# Level 2 (spatial):  Spatial border/center split + mean-shift simplification
#                     + agglomerative Ward clustering.
#
# Level 1 is used when a SAM2 mask_generator is supplied; Level 2 is the
# automatic fallback when SAM2 is not configured or finds no usable masks.
#
# Output schema
# -------------
# Both foreground and background dicts carry:
#   "rgb"       : [R, G, B]          – dominant colour
#   "lab"       : [L*, a*, b*]
#   "luminance" : float              – L*/100
#   "chroma"    : float              – C*/100
#   "palette"   : list[colour_dict]  – up to _FIG_N_PALETTE colours, ranked
#   "coverage"  : float              – fraction of frame pixels in this region


def _mean_shift_simplify(arr: np.ndarray) -> np.ndarray:
    """Merge fine texture into coherent colour regions via mean-shift filtering.

    Wraps ``cv2.pyrMeanShiftFiltering`` when OpenCV is available; returns the
    original array unchanged when it is not (graceful degradation).

    Parameters
    ----------
    arr : (H, W, 3) uint8 RGB array.

    Returns
    -------
    (H, W, 3) uint8 RGB simplified array — same shape as input.
    """
    try:
        import cv2  # type: ignore
    except ImportError:
        return arr
    return cv2.pyrMeanShiftFiltering(arr, sp=_FIG_MS_SP, sr=_FIG_MS_SR)


def _null_colour_dict() -> dict:
    """Zero-value colour dict used as a sentinel for empty regions."""
    return {
        "rgb":       (0, 0, 0),
        "lab":       [0.0, 0.0, 0.0],
        "luminance": 0.0,
        "chroma":    0.0,
        "weight":    0.0,
        "size":      0,
    }


def _agglomerative_palette(
    pixels_rgb: np.ndarray,
    n_clusters: int = _FIG_N_CLUSTERS,
    n_palette: int = _FIG_N_PALETTE,
) -> "list[dict]":
    """Agglomerative Ward clustering in LAB space for rich palette extraction.

    More perceptually stable than k-means: bottom-up merging avoids local-
    minima sensitivity and never requires a random seed.

    Algorithm
    ---------
    1. Stride-sample up to ``_FIG_MAX_SAMPLE`` pixels (deterministic).
    2. Convert to CIELAB; run Ward linkage → *k* clusters.
    3. Assign ALL pixels to the nearest sample-cluster centroid (Voronoi).
    4. Rank clusters by total perceptual weight; return top *n_palette*.

    Returns
    -------
    list[dict]
        Same schema as ``_rank_region_candidates``:
        ``rgb``, ``lab``, ``luminance``, ``chroma``, ``weight``, ``size``.
        Falls back to ``_rank_region_candidates`` when scipy is unavailable.
    """
    if len(pixels_rgb) == 0:
        return [_null_colour_dict()]

    try:
        from scipy.cluster.hierarchy import linkage, fcluster  # type: ignore
    except ImportError:
        # scipy not available: fall back to existing k-means
        return _rank_region_candidates(pixels_rgb, n_clusters)[:n_palette]

    # --- Stride-sample for fast Ward linkage ---------------------------------
    n = len(pixels_rgb)
    if n > _FIG_MAX_SAMPLE:
        step = max(1, n // _FIG_MAX_SAMPLE)
        idx_sample = np.arange(0, n, step)[:_FIG_MAX_SAMPLE]
    else:
        idx_sample = np.arange(n)

    sample_rgb = pixels_rgb[idx_sample]
    sample_lab = _rgb_to_lab(sample_rgb)

    k = min(n_clusters, len(idx_sample))
    if k < 2:
        return _rank_region_candidates(pixels_rgb, 1)[:n_palette]

    # --- Ward linkage on sample LAB values -----------------------------------
    try:
        Z = linkage(sample_lab.astype(np.float64), method="ward")
        sample_labels = fcluster(Z, t=k, criterion="maxclust")  # 1-indexed
    except Exception:
        return _rank_region_candidates(pixels_rgb, n_clusters)[:n_palette]

    # --- Cluster centroids (mean LAB per sample cluster) ---------------------
    centroids_lab = np.zeros((k, 3), dtype=np.float64)
    for cid in range(1, k + 1):
        cluster_mask = sample_labels == cid
        if cluster_mask.any():
            centroids_lab[cid - 1] = sample_lab[cluster_mask].mean(axis=0)

    # --- Assign ALL pixels to nearest centroid (Voronoi) ---------------------
    all_lab = _rgb_to_lab(pixels_rgb)
    diffs = all_lab[:, np.newaxis, :] - centroids_lab[np.newaxis, :, :]  # (N, k, 3)
    dists_sq = (diffs ** 2).sum(axis=2)                                   # (N, k)
    assigned = np.argmin(dists_sq, axis=1) + 1                            # 1-indexed

    weights = _perceptual_weights(all_lab)

    # --- Cluster statistics --------------------------------------------------
    candidates: list[dict] = []
    for cid in range(1, k + 1):
        cluster_mask = assigned == cid
        count = int(cluster_mask.sum())
        if count == 0:
            continue
        cluster_weight = float(weights[cluster_mask].sum())
        mean_rgb = pixels_rgb[cluster_mask].astype(np.float64).mean(axis=0)
        mean_lab = all_lab[cluster_mask].mean(axis=0)
        chroma = float(np.sqrt(float(mean_lab[1]) ** 2 + float(mean_lab[2]) ** 2))
        candidates.append({
            "rgb":       (
                int(round(float(mean_rgb[0]))),
                int(round(float(mean_rgb[1]))),
                int(round(float(mean_rgb[2]))),
            ),
            "lab":       [
                round(float(mean_lab[0]), 1),
                round(float(mean_lab[1]), 1),
                round(float(mean_lab[2]), 1),
            ],
            "luminance": round(float(mean_lab[0]) / 100.0, 3),
            "chroma":    round(chroma / 100.0, 3),
            "weight":    cluster_weight,
            "size":      count,
        })

    if not candidates:
        return [_null_colour_dict()]

    candidates.sort(key=lambda c: c["weight"], reverse=True)
    return candidates[:n_palette]


def _region_info_from_candidates(
    candidates: "list[dict]",
    total_pixels: int,
) -> dict:
    """Build the extended region dict from a ranked candidate list.

    The top candidate becomes the dominant colour.  All candidates form the
    ``"palette"`` list (``weight`` / ``size`` stripped for output).
    ``"coverage"`` is the region-pixel fraction of the full frame.
    """
    if not candidates:
        c = _null_colour_dict()
        return {
            "rgb":       list(c["rgb"]),
            "lab":       c["lab"],
            "luminance": c["luminance"],
            "chroma":    c["chroma"],
            "palette":   [],
            "coverage":  0.0,
        }
    top = candidates[0]
    region_size = sum(c.get("size", 0) for c in candidates)
    coverage = round(region_size / max(1, total_pixels), 4)
    palette = [
        {
            "rgb":       list(c["rgb"]),
            "lab":       c["lab"],
            "luminance": c["luminance"],
            "chroma":    c["chroma"],
        }
        for c in candidates
    ]
    return {
        "rgb":       list(top["rgb"]),
        "lab":       top["lab"],
        "luminance": top["luminance"],
        "chroma":    top["chroma"],
        "palette":   palette,
        "coverage":  coverage,
    }


def _spatial_masks(height: int, width: int, border: int) -> "tuple[np.ndarray, np.ndarray]":
    """Return (fg_mask, bg_mask) 2-D bool arrays.

    ``fg_mask`` is the inner rectangle; ``bg_mask`` is the outer border strip.
    """
    bg = np.zeros((height, width), dtype=bool)
    bg[:border, :]             = True
    bg[height - border:, :]    = True
    bg[:, :border]             = True
    bg[:, width - border:]     = True
    return ~bg, bg


def _sam2_fg_bg_masks(
    arr_rgb: np.ndarray,
    mask_generator,
) -> "tuple[np.ndarray, np.ndarray, float]":
    """Classify pixels as foreground / background using SAM2 automatic masks.

    Foreground = masks that do **not** touch any frame edge AND whose
    bounding-box centre lies in the inner 60 % of the frame.  Everything
    else (edge-touching masks, uncovered pixels) is background.

    Returns
    -------
    (fg_mask, bg_mask, confidence)
        fg_mask, bg_mask — (H, W) bool arrays.
        confidence       — fraction of frame pixels covered by accepted fg masks.
    """
    from services.silhouette import check_containment  # type: ignore

    h, w = arr_rgb.shape[:2]
    fg_mask = np.zeros((h, w), dtype=bool)
    bg_mask = np.ones((h, w), dtype=bool)

    try:
        masks = mask_generator.generate(arr_rgb)
    except Exception:
        return fg_mask, bg_mask, 0.0

    cy_lo, cy_hi = int(h * 0.20), int(h * 0.80)
    cx_lo, cx_hi = int(w * 0.20), int(w * 0.80)

    for m in masks:
        seg = np.asarray(m["segmentation"], dtype=bool)
        if not check_containment(seg):
            continue
        bbox = m.get("bbox", [])
        if len(bbox) >= 4:
            bx, by, bw_m, bh_m = bbox
            cx_centre = bx + bw_m / 2
            cy_centre = by + bh_m / 2
            if not (cx_lo <= cx_centre <= cx_hi and cy_lo <= cy_centre <= cy_hi):
                continue
        fg_mask |= seg
        bg_mask &= ~seg

    confidence = round(float(fg_mask.sum()) / float(h * w), 4)
    return fg_mask, bg_mask, confidence


def _extract_fg_bg_figure(
    arr_rgb: np.ndarray,
    *,
    mask_generator=None,
) -> "tuple[dict, dict, dict]":
    """Full figure-ground extraction pipeline.

    Parameters
    ----------
    arr_rgb         : (H, W, 3) uint8 RGB array (already resized).
    mask_generator  : Optional SAM2AutomaticMaskGenerator for semantic split.

    Returns
    -------
    (foreground_dict, background_dict, diagnostics_dict)
        foreground / background have the extended schema with ``"palette"``
        and ``"coverage"`` in addition to the standard keys.
        diagnostics_dict carries all rescue-pass keys plus
        ``"method_used"``, ``"segmentation_used"``,
        ``"segmentation_confidence"``, ``"superpixels_used"``,
        ``"fg_region_count"``, ``"bg_region_count"``,
        ``"cluster_count"``, ``"fallback_level"``.
    """
    h, w = arr_rgb.shape[:2]
    total_pixels = h * w

    # Step 1 — mean-shift simplification
    simplified = _mean_shift_simplify(arr_rgb)
    superpixels_used = not np.array_equal(simplified, arr_rgb)

    # Step 2 — semantic segmentation or spatial fallback
    segmentation_confidence = 0.0
    if mask_generator is not None:
        fg_mask, bg_mask, segmentation_confidence = _sam2_fg_bg_masks(
            arr_rgb, mask_generator
        )
        if fg_mask.sum() < total_pixels * 0.02:
            # SAM2 found almost nothing: fall back
            border = max(1, h // 4)
            fg_mask, bg_mask = _spatial_masks(h, w, border)
            fallback_level = 2
            segmentation_used = "spatial"
        else:
            fallback_level = 1
            segmentation_used = "sam2"
    else:
        border = max(1, h // 4)
        fg_mask, bg_mask = _spatial_masks(h, w, border)
        fallback_level = 2
        segmentation_used = "spatial"

    fg_pixels = simplified[fg_mask]
    bg_pixels = simplified[bg_mask]

    # Step 3 — agglomerative clustering per region
    fg_candidates = _agglomerative_palette(fg_pixels, _FIG_N_CLUSTERS, _FIG_N_PALETTE)
    bg_candidates = _agglomerative_palette(bg_pixels, _FIG_N_CLUSTERS, _FIG_N_PALETTE)

    # Step 4 — rescue pass (reuses existing logic)
    fg_top, bg_top, rescue_diag = _maybe_rescue_pair(fg_candidates, bg_candidates)

    # Build extended output dicts, then override dominant with rescue result
    fg_out = _region_info_from_candidates(fg_candidates, total_pixels)
    bg_out = _region_info_from_candidates(bg_candidates, total_pixels)

    for key in ("rgb", "lab", "luminance", "chroma"):
        if key in fg_top:
            fg_out[key] = fg_top[key]
        if key in bg_top:
            bg_out[key] = bg_top[key]

    # Step 5 — diagnostics
    diagnostics: "dict[str, Any]" = {
        **rescue_diag,
        "method_used":             "figure",
        "segmentation_used":       segmentation_used,
        "segmentation_confidence": segmentation_confidence,
        "superpixels_used":        superpixels_used,
        "fg_region_count":         int(fg_mask.sum()),
        "bg_region_count":         int(bg_mask.sum()),
        "cluster_count":           _FIG_N_CLUSTERS,
        "fallback_level":          fallback_level,
    }
    return fg_out, bg_out, diagnostics


def _extract_fg_bg_full(
    image_path: Path,
    *,
    sam_mask_generator=None,
) -> "tuple[dict, dict, dict]":
    """Full figure-ground extraction returning ``(foreground, background, diagnostics)``.

    Parameters
    ----------
    image_path        : Path to a PNG or other Pillow-readable image.
    sam_mask_generator: Optional SAM2AutomaticMaskGenerator for semantic
                        figure-ground segmentation.  When ``None``, the
                        pipeline falls back to the spatial border/center split.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow is required for palette extraction.  "
            "Install with:  pip install Pillow"
        ) from exc

    arr = np.asarray(
        Image.open(image_path)
        .convert("RGB")
        .resize((_FIG_RESIZE, _FIG_RESIZE), Image.LANCZOS)
    )
    return _extract_fg_bg_figure(arr, mask_generator=sam_mask_generator)


def extract_fg_bg(
    image_path: Path,
    *,
    sam_mask_generator=None,
) -> "tuple[dict, dict]":
    """Extract dominant foreground and background colours from a frame image.

    Parameters
    ----------
    image_path        : Path to a PNG (or any Pillow-readable) image.
    sam_mask_generator: Optional SAM2AutomaticMaskGenerator for semantic
                        figure-ground segmentation.

    Returns
    -------
    (foreground_dict, background_dict)
        Each dict has ``"rgb"``, ``"lab"``, ``"luminance"``, ``"chroma"``,
        ``"palette"``, and ``"coverage"`` keys.

    Raises
    ------
    ImportError  If Pillow is not installed.
    OSError      If the image cannot be opened.
    """
    fg, bg, _diag = _extract_fg_bg_full(
        image_path, sam_mask_generator=sam_mask_generator
    )
    return fg, bg


# ---------------------------------------------------------------------------
# Per-shot palette extraction
# ---------------------------------------------------------------------------

def _process_one_shot(
    project_path: str,
    filename: str,
    media_type: str,
    entry: dict,
    shot_index: int,
    shot_info: dict | None,
    *,
    sam_mask_generator=None,
) -> dict:
    """Extract palette for one annotation entry.

    Returns a shot-palette dict (always, even on skip/error).
    The ``"status"`` key will be ``"ok"``, ``"skipped"``, or ``"error"``.
    """
    from services.frame_match import best_frame_path

    shot_data = entry.get("shot", {})
    shot_id = str(shot_data.get("shot_id", ""))

    base: dict[str, Any] = {
        "shot_id": shot_id,
        "shot_index": shot_index,
        "start_time": (shot_info or {}).get("start_time", ""),
        "end_time": (shot_info or {}).get("end_time", ""),
    }

    best_frame_meta = shot_data.get("best_frame")
    if not best_frame_meta:
        return {**base, "status": "skipped", "reason": "no best_frame in annotation"}

    frame_number = best_frame_meta.get("frame")
    if frame_number is None:
        return {**base, "status": "skipped", "reason": "best_frame has no frame number"}

    base["best_frame_number"] = frame_number

    png_path = best_frame_path(project_path, media_type, filename, shot_id)
    if not png_path.exists():
        return {**base, "status": "skipped", "reason": f"best_frame PNG not found: {png_path}"}

    try:
        foreground, background, diagnostics = _extract_fg_bg_full(
            png_path, sam_mask_generator=sam_mask_generator
        )
    except Exception as exc:
        return {**base, "status": "error", "reason": str(exc)}

    result: dict = {
        **base,
        "foreground": foreground,
        "background": background,
        "method": "figure",
        "confidence": 1.0,
        "status": "ok",
    }
    if diagnostics:
        result["diagnostics"] = diagnostics
    return result


# ---------------------------------------------------------------------------
# Movie-level palette creation
# ---------------------------------------------------------------------------

def create_palette_for_movie(
    project_path: str,
    filename: str,
    media_type: str = "movies",
    *,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Build and cache a palette for every shot in *filename*.

    Reads the annotation JSON and shotlist; for each shot that has a
    ``best_frame`` PNG, extracts foreground and background colours using
    the figure-ground pipeline.

    The SAM2 segmentation model is loaded automatically from the project's
    ``model_segmentation`` preference
    (``crossing tool model set segmentation <name>``).
    If no model is configured the pipeline falls back to the spatial
    border/center split.

    Returns a summary dict with keys:
        ``filename``, ``shot_count``, ``processed``, ``skipped``, ``failed``

    Raises:
        FileExistsError: If the palette cache already exists and *force* is False.
        FileNotFoundError: If the annotation JSON does not exist.
    """
    cache_path = get_palette_path(project_path, filename, media_type)
    _overwrite = force  # True if we are authorized to overwrite an existing cache
    if cache_path.exists() and not force:
        # Check whether the shotlist is newer than the palette cache (dirty check).
        shotlist_path = get_shotlist_path(project_path, filename, media_type)
        stale = (
            shotlist_path.exists()
            and shotlist_path.stat().st_mtime > cache_path.stat().st_mtime
        )
        if not stale:
            if verbose:
                print(f"  skip  {filename}: palette cache already exists (use --force to overwrite)")
            return {
                "filename": filename,
                "shot_count": 0,
                "processed": 0,
                "skipped": 0,
                "failed": 0,
                "cached": True,
            }
        # Stale: the shotlist is newer — regeneration is authorised, so we must
        # overwrite the existing file even though the caller did not pass --force.
        _overwrite = True
        if verbose:
            print(f"  stale {filename}: shotlist is newer than palette cache — regenerating")

    # Load annotation entries
    entries = load_annotation_items(project_path, filename, media_type)

    # Resolve movie metadata
    meta_entries = get_metadata(project_path, media_type=media_type)
    meta = next((e for e in meta_entries if e.get("filename") == filename), {})
    media_id = compute_media_id(meta, media_type)

    movie_block: dict[str, Any] = {
        "filename": filename,
        "media_type": media_type,
        "title": meta.get("title") or Path(filename).stem,
        "year": meta.get("year"),
    }

    # Build shot_id → shotlist row mapping for timing data (best-effort)
    shots_by_id: dict[str, dict] = {}
    try:
        shotlist = read_shotlist(project_path, filename, media_type)
        for shot in shotlist:
            sf = int(shot.get("start_frame") or 0)
            ef = int(shot.get("end_frame") or 0)
            sid = build_shot_id(media_id, sf, ef)
            shots_by_id[sid] = shot
    except FileNotFoundError:
        if verbose:
            print(f"  warn  {filename}: shotlist not found; timing fields will be empty")

    # Process each shot
    shot_palettes: list[dict] = []
    processed = 0
    skipped = 0
    failed = 0

    # Load SAM2 once for the whole movie (if configured in project prefs).
    sam_mask_generator = None
    from tool import prefs as _prefs
    _sam_name = _prefs.get("model_segmentation")
    if _sam_name:
        try:
            from services.silhouette import load_sam_model  # type: ignore
            sam_mask_generator, _, _dev = load_sam_model(project_path, _sam_name)
            if verbose:
                print(f"  SAM2 loaded: {_sam_name} ({_dev})")
        except (ImportError, FileNotFoundError, RuntimeError) as _sam_exc:
            if verbose:
                print(f"  warn  SAM2 not available ({_sam_exc}); using spatial fallback")
    elif verbose:
        print("  info  no segmentation model configured; using spatial fallback")

    for i, entry in enumerate(entries):
        shot_data = entry.get("shot", {})
        shot_id = str(shot_data.get("shot_id", ""))
        shot_info = shots_by_id.get(shot_id)

        result = _process_one_shot(
            project_path, filename, media_type, entry, i, shot_info,
            sam_mask_generator=sam_mask_generator,
        )

        status = result.pop("status", "ok")

        if status == "ok":
            processed += 1
            if verbose:
                fg = result.get("foreground", {}).get("rgb", [])
                bg = result.get("background", {}).get("rgb", [])
                print(f"  ok    shot {i:4d}  fg={fg}  bg={bg}")
        elif status == "skipped":
            skipped += 1
            if verbose:
                reason = result.pop("reason", "")
                print(f"  skip  shot {i:4d}  {reason}")
            else:
                result.pop("reason", None)
        elif status == "error":
            failed += 1
            reason = result.pop("reason", "")
            print(f"  fail  {filename} shot {i}: {reason}", flush=True)

        shot_palettes.append(result)

    summary = {
        "shot_count": len(entries),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
    }

    palette_doc: dict[str, Any] = {
        "movie": movie_block,
        "source": "best_frame",
        "method": "figure",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "shots": shot_palettes,
        "summary": summary,
    }

    save_palette(project_path, filename, media_type, palette_doc, force=_overwrite)

    if verbose:
        print(
            f"  → saved  {cache_path.relative_to(project_path) if project_path else cache_path}"
        )

    return {"filename": filename, "cached": False, **summary}


def create_palette_for_all_movies(
    project_path: str,
    media_type: str = "movies",
    *,
    force: bool = False,
    verbose: bool = False,
    on_item_done=None,
) -> dict:
    """Build palette caches for every movie that has an annotation JSON.

    Returns a summary dict with aggregated counts and a per-film ``results``
    list.
    """
    entries = get_metadata(project_path, media_type=media_type)
    filenames = [e["filename"] for e in entries if e.get("filename")]

    total_processed = 0
    total_skipped = 0
    total_failed = 0
    total_cached = 0
    results: list[dict] = []

    for filename in filenames:
        try:
            summary = create_palette_for_movie(
                project_path,
                filename,
                media_type,
                force=force,
                verbose=verbose,
            )
        except FileNotFoundError as exc:
            print(f"  skip  {filename}: {exc}", flush=True)
            results.append({"filename": filename, "skipped": True, "reason": str(exc)})
            if on_item_done is not None:
                on_item_done(filename, None, exc)
            continue
        except FileExistsError as exc:
            # Should not happen because we handle cache-exists inside
            # create_palette_for_movie, but guard here anyway.
            print(f"  skip  {filename}: {exc}", flush=True)
            results.append({"filename": filename, "skipped": True, "reason": str(exc)})
            if on_item_done is not None:
                on_item_done(filename, None, exc)
            continue
        except Exception as exc:
            print(f"  fail  {filename}: {exc}", flush=True)
            results.append({"filename": filename, "error": str(exc)})
            if on_item_done is not None:
                on_item_done(filename, None, exc)
            continue

        if summary.get("cached"):
            total_cached += 1
        else:
            total_processed += summary.get("processed", 0)
            total_skipped += summary.get("skipped", 0)
            total_failed += summary.get("failed", 0)
        results.append(summary)
        if on_item_done is not None:
            on_item_done(filename, summary, None)

    return {
        "media_type": media_type,
        "total_files": len(filenames),
        "total_processed": total_processed,
        "total_skipped": total_skipped,
        "total_failed": total_failed,
        "total_cached": total_cached,
        "results": results,
    }
