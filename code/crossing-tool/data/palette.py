"""Palette indexing: dominant foreground/background colour extraction from best frames.

Cache path
----------
    <project>/data/index/palette/<media_type>/<stem>.json

For each shot in a film the best-frame PNG (written by ``crossing annotate
frame``) is loaded, resized to 128×128 and split into two non-overlapping
spatial regions.  The dominant colour in each region is returned as an RGB
triplet plus perceptual metadata.

Algorithm — LAB k-means weighted  (default, ``method="lab"``)
-------------------------------------------------------------
1. Load the PNG with Pillow; convert to RGB; resize to 128×128 (LANCZOS).
2. Split into two regions:
   - border : outer 32-px strip  → background
   - center : inner 64×64 block  → foreground
3. For each region:
   a. Convert pixels sRGB → CIELAB (D65, pure-numpy, no extra deps).
   b. Compute per-pixel chroma  C* = √(a*² + b*²).
   c. Adaptive dark threshold: if >70 % of pixels have L* < 8 (dark scene),
      lower the effective threshold to 2.0 so moonlit blues / noir shadows
      are preserved rather than collapsed to black.
   d. Per-pixel weight = (lum_ramp + 0.1) × (1 + chroma_factor), where
      lum_ramp is a smooth 0→1 ramp above the effective L* threshold and
      chroma_factor = min(C*/30, 2.0).  Saturated highlights therefore
      outweigh numerically dominant dark walls.
   e. Run deterministic k-means (k = 5) in LAB space; centres are
      initialised by stratified sampling along the L* axis — no random seed.
   f. Rank clusters by total perceptual weight; take the winning cluster's
      mean RGB.  Return also LAB centroid, luminance, and chroma scores.
4. background = winning colour from border region
   foreground = winning colour from center region

Algorithm — simple / legacy  (``method="simple"``)
---------------------------------------------------
The original RGB-quantisation method is preserved verbatim as
``_extract_dominant_colour()``.  Pass ``method="simple"`` to
``extract_fg_bg()`` for research comparison against the new method.

Tuning constants
----------------
``_LAB_MIN_L``          near-black L* threshold            (default 8.0)
``_LAB_DARK_FRACTION``  dark-scene detection fraction      (default 0.70)
``_N_CLUSTERS``         k-means cluster count              (default 5)
``_KMEANS_MAX_ITER``    maximum k-means iterations         (default 20)

Output shape (method="lab")
---------------------------
::

    "foreground": {
        "rgb":       [80, 92, 130],
        "lab":       [42.1, 5.3, -22.0],
        "luminance": 0.421,          # L* / 100
        "chroma":    0.229           # sqrt(a*²+b*²) / 100
    }

No random state, no model, no network — output is fully deterministic.

Dependencies: Pillow (core dep), numpy (core dep).
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
from data.shotlist import read_shotlist

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RESIZE = 128           # target size after resize
_BORDER = _RESIZE // 4  # 32-pixel outer strip

# Perceptual extraction tuning
_LAB_MIN_L         = 8.0   # L* below this is "near-black" and gets downweighted
_LAB_DARK_FRACTION = 0.70  # if this fraction of pixels is near-black, lower threshold
_N_CLUSTERS        = 5     # number of k-means clusters per spatial region
_KMEANS_MAX_ITER   = 20    # max k-means iterations (converges well before this)

# Low-key rescue pass thresholds
_NEAR_BLACK_L               = 15.0  # L* below this is "near-black"
_NEAR_BLACK_CHROMA          = 10.0  # C* below this (with low L*) = effectively black
_RESCUE_TRIGGER_MAX_L       = 20.0  # rescue triggers when both L* values are below this
_RESCUE_TRIGGER_MIN_DELTA_E = 15.0  # rescue triggers when pair CIE76 ΔE is below this


# ---------------------------------------------------------------------------
# Cache path helpers
# ---------------------------------------------------------------------------

def get_palette_path(project_path: str, filename: str, media_type: str) -> Path:
    """Return the canonical cache path for a palette JSON.

    ``<project>/data/index/palette/<media_type>/<stem>.json``
    """
    stem = Path(filename).stem
    return Path(project_path) / "data" / "index" / "palette" / media_type / f"{stem}.json"


def load_palette(project_path: str, filename: str, media_type: str) -> dict | None:
    """Load a cached palette JSON or return ``None`` if absent."""
    path = get_palette_path(project_path, filename, media_type)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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

def _extract_dominant_colour(pixels: np.ndarray) -> tuple[int, int, int]:
    """Legacy: dominant colour by RGB-bin frequency.

    Preserved unchanged for research comparison.  Accessible via
    ``extract_fg_bg(..., method="simple")``.

    Steps:
    1. Quantise each channel to 8 bins (right-shift 5 bits → 0–7).
    2. Encode as r*64 + g*8 + b; find the most-common bin.
    3. Return the mean RGB of pixels in that bin.
    """
    if len(pixels) == 0:
        return (0, 0, 0)

    bins  = (pixels >> 5).astype(np.int32)  # 0–7 per channel
    codes = bins[:, 0] * 64 + bins[:, 1] * 8 + bins[:, 2]

    unique, counts = np.unique(codes, return_counts=True)
    best_code = unique[np.argmax(counts)]

    mask = codes == best_code
    mean = pixels[mask].mean(axis=0)
    return (int(round(mean[0])), int(round(mean[1])), int(round(mean[2])))


def _extract_perceptual_colour(
    pixels_rgb: np.ndarray,
) -> "tuple[tuple[int, int, int], dict]":
    """Dominant colour via LAB k-means with perceptual weighting.

    Returns ``(rgb_tuple, metadata)`` for the top-ranked cluster.
    Preserved for backward compatibility; internal code uses
    ``_rank_region_candidates`` directly.
    """
    best = _rank_region_candidates(pixels_rgb, _N_CLUSTERS)[0]
    return best["rgb"], {
        "lab":       best["lab"],
        "luminance": best["luminance"],
        "chroma":    best["chroma"],
    }


def _extract_fg_bg_full(
    image_path: Path,
    *,
    method: str = "lab",
) -> "tuple[dict, dict, dict]":
    """Full extraction returning ``(foreground, background, diagnostics)``.

    ``diagnostics`` is non-empty only for ``method="lab"``; it is always a
    dict.  See ``_maybe_rescue_pair`` for the diagnostics schema.
    """
    if method not in {"lab", "simple"}:
        raise ValueError(
            f"Unknown extraction method {method!r}; use 'lab' or 'simple'."
        )

    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow is required for palette extraction.  "
            "Install with:  pip install Pillow"
        ) from exc

    img = (
        Image.open(image_path)
        .convert("RGB")
        .resize((_RESIZE, _RESIZE), Image.LANCZOS)
    )
    arr = np.asarray(img)  # (128, 128, 3) uint8

    # Spatial masks
    border_mask = np.zeros((_RESIZE, _RESIZE), dtype=bool)
    border_mask[:_BORDER, :]             = True
    border_mask[_RESIZE - _BORDER:, :]  = True
    border_mask[:, :_BORDER]             = True
    border_mask[:, _RESIZE - _BORDER:]  = True
    center_mask = ~border_mask

    border_pixels = arr[border_mask]  # (N, 3)
    center_pixels = arr[center_mask]  # (M, 3)

    if method == "simple":
        bg_rgb = _extract_dominant_colour(border_pixels)
        fg_rgb = _extract_dominant_colour(center_pixels)
        return {"rgb": list(fg_rgb)}, {"rgb": list(bg_rgb)}, {}

    # LAB method: rank candidates per region then apply rescue pass
    fg_candidates = _rank_region_candidates(center_pixels)
    bg_candidates = _rank_region_candidates(border_pixels)
    foreground, background, diagnostics = _maybe_rescue_pair(fg_candidates, bg_candidates)
    return foreground, background, diagnostics


def extract_fg_bg(
    image_path: Path,
    *,
    method: str = "lab",
) -> "tuple[dict, dict]":
    """Extract dominant foreground and background colours from a frame image.

    Parameters
    ----------
    image_path : Path
        Path to a PNG (or any Pillow-readable) image.
    method : {"lab", "simple"}
        ``"lab"``    — perceptual LAB k-means with low-key rescue (default).
        ``"simple"`` — legacy RGB-bin method; kept for research comparison.

    Returns
    -------
    (foreground_dict, background_dict)
        Each dict always has an ``"rgb"`` key (``[R, G, B]`` list, 0–255).
        The ``"lab"`` method also adds ``"lab"``, ``"luminance"``,
        and ``"chroma"`` keys.

    Raises
    ------
    ImportError  If Pillow is not installed.
    OSError      If the image cannot be opened.
    ValueError   If *method* is not recognised.

    Note
    ----
    Per-shot diagnostics (rescue metadata, pair quality scores) are available
    via the internal ``_extract_fg_bg_full`` function.
    """
    fg, bg, _diag = _extract_fg_bg_full(image_path, method=method)
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
    method: str = "lab",
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
        foreground, background, diagnostics = _extract_fg_bg_full(png_path, method=method)
    except Exception as exc:
        return {**base, "status": "error", "reason": str(exc)}

    method_name = "lab_kmeans_weighted" if method == "lab" else "border_center_dominant"
    result: dict = {
        **base,
        "foreground": foreground,
        "background": background,
        "method": method_name,
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
    method: str = "lab",
) -> dict:
    """Build and cache a palette for every shot in *filename*.

    Reads the annotation JSON and shotlist; for each shot that has a
    ``best_frame`` PNG, extracts foreground and background colours.

    Returns a summary dict with keys:
        ``filename``, ``shot_count``, ``processed``, ``skipped``, ``failed``

    Raises:
        FileExistsError: If the palette cache already exists and *force* is False.
        FileNotFoundError: If the annotation JSON does not exist.
    """
    cache_path = get_palette_path(project_path, filename, media_type)
    if cache_path.exists() and not force:
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

    for i, entry in enumerate(entries):
        shot_data = entry.get("shot", {})
        shot_id = str(shot_data.get("shot_id", ""))
        shot_info = shots_by_id.get(shot_id)

        result = _process_one_shot(
            project_path, filename, media_type, entry, i, shot_info,
            method=method,
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

    extraction_method = "lab_kmeans_weighted" if method == "lab" else "border_center_dominant"
    palette_doc: dict[str, Any] = {
        "movie": movie_block,
        "source": "best_frame",
        "method": extraction_method,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "shots": shot_palettes,
        "summary": summary,
    }

    save_palette(project_path, filename, media_type, palette_doc, force=force)

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
    method: str = "lab",
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
                method=method,
            )
        except FileNotFoundError as exc:
            print(f"  skip  {filename}: {exc}", flush=True)
            results.append({"filename": filename, "skipped": True, "reason": str(exc)})
            continue
        except FileExistsError as exc:
            # Should not happen because we handle cache-exists inside
            # create_palette_for_movie, but guard here anyway.
            print(f"  skip  {filename}: {exc}", flush=True)
            results.append({"filename": filename, "skipped": True, "reason": str(exc)})
            continue
        except Exception as exc:
            print(f"  fail  {filename}: {exc}", flush=True)
            results.append({"filename": filename, "error": str(exc)})
            continue

        if summary.get("cached"):
            total_cached += 1
        else:
            total_processed += summary.get("processed", 0)
            total_skipped += summary.get("skipped", 0)
            total_failed += summary.get("failed", 0)
        results.append(summary)

    return {
        "media_type": media_type,
        "total_files": len(filenames),
        "total_processed": total_processed,
        "total_skipped": total_skipped,
        "total_failed": total_failed,
        "total_cached": total_cached,
        "results": results,
    }
