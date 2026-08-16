"""Palette indexing: figure-ground colour extraction from project images.

Cache paths
-----------
Best-frame palettes retain their existing per-film path::

    <project>/data/palettes/<media_type>/<stem>.json

Thumbnail palettes use canonical media identity::

    <project>/data/palettes/<media_type>/thumbnail/<media_id>.json

For each shot in a film the best-frame PNG (written by ``crossing annotate
frame``) is loaded and analysed using the figure-ground palette pipeline.
Thumbnail palettes run that same pipeline once on the exact image resolved by
the Metadata Viewer.

Algorithm — figure
------------------
1. Detect paired, continuous near-black bars at the original image edges.
2. Crop to the detected active-image bounds, or retain the full image when no
    bars are detected.
3. Resize the active image to 256×256 (LANCZOS) for texture detail.
4. Simplify texture via ``cv2.pyrMeanShiftFiltering``.
5. Request foreground-object masks through the configured segmentation model's
    canonical ``segment_palette(image_pil)`` interface; masks that do not touch
    the frame edge and whose bounding-box centre lies within the inner 60 % of
    the frame are classified as foreground.
6. Cluster each region with agglomerative Ward clustering in CIELAB space
    using scipy.
7. Rank clusters by perceptual weight; apply a rescue pass when both
   dominant colours are near-black or insufficiently distinct.

Missing dependencies, unavailable/incompatible segmentation models, inference
errors, and unusable segmentation results are explicit analysis failures. The
palette pipeline does not probe alternate model APIs or use a spatial,
clustering, or sentinel-colour substitute.

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

Output is deterministic for a fixed segmentation result.

Dependencies: Pillow (core dep), numpy (core dep).
Required for extraction: cv2 (OpenCV), scipy, and a configured segmentation
adapter exposing ``segment_palette(image_pil)``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from data.index import load_annotation_items
from data.media_id import build_shot_id, compute_media_id
from data.metadata import get_metadata, resolve_thumbnail_path
from data.shotlist import get_shotlist_path, read_shotlist

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Perceptual weighting tuning
_LAB_MIN_L         = 8.0   # L* below this is "near-black" and gets downweighted
_LAB_DARK_FRACTION = 0.70  # if this fraction of pixels is near-black, lower threshold

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

# Conservative edge-connected letterbox / pillarbox detection
_LETTERBOX_NEAR_BLACK_LUMA = 16
_LETTERBOX_MAX_CHANNEL_SPREAD = 8
_LETTERBOX_DARK_COVERAGE = 0.98
_LETTERBOX_MIN_THICKNESS_FRACTION = 0.015
_LETTERBOX_MIN_THICKNESS_PIXELS = 2
_LETTERBOX_INTERIOR_PROBE_FRACTION = 0.01
_LETTERBOX_INTERIOR_PROBE_MAX_PIXELS = 16
_LETTERBOX_MAX_INTERIOR_DARK_COVERAGE = 0.90
_LETTERBOX_MIN_INTERIOR_LUMA_DELTA = 20
_LETTERBOX_MAX_SAMPLES = 512


class PaletteAnalysisError(RuntimeError):
    """The intended figure-ground analysis could not produce a valid palette."""


def _edge_run_length(flags: np.ndarray) -> int:
    """Return the number of consecutive true values from the first edge."""
    first_false = np.flatnonzero(~flags)
    return int(first_false[0]) if len(first_false) else int(len(flags))


def _edge_has_content_transition(
    luma: np.ndarray,
    near_black: np.ndarray,
    run_length: int,
    min_thickness: int,
) -> bool:
    """Return whether one edge run has strong evidence of adjacent content."""
    axis_length = luma.shape[0]
    if run_length < min_thickness or run_length >= axis_length:
        return False

    probe_size = max(
        min_thickness,
        int(np.ceil(axis_length * _LETTERBOX_INTERIOR_PROBE_FRACTION)),
    )
    probe_size = min(probe_size, _LETTERBOX_INTERIOR_PROBE_MAX_PIXELS)
    probe_end = min(axis_length, run_length + probe_size)
    if probe_end <= run_length:
        return False

    bar_luma = float(luma[:run_length].mean())
    interior_luma = float(luma[run_length:probe_end].mean())
    interior_dark_coverage = float(
        near_black[run_length:probe_end].mean()
    )
    return (
        interior_dark_coverage <= _LETTERBOX_MAX_INTERIOR_DARK_COVERAGE
        and interior_luma - bar_luma >= _LETTERBOX_MIN_INTERIOR_LUMA_DELTA
    )


def _detect_content_bbox(image) -> dict:
    """Return conservative active-image bounds in original image coordinates.

    Top, bottom, left, and right are evaluated independently. Each accepted bar
    must be a meaningful continuous edge run followed by a strong transition to
    non-bar image content. No accepted bars returns the full image bounds.
    """
    try:
        from PIL import Image

        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError(f"invalid image dimensions {width}x{height}")
        rgb_image = image if image.mode == "RGB" else image.convert("RGB")
        sample_width = min(width, _LETTERBOX_MAX_SAMPLES)
        sample_height = min(height, _LETTERBOX_MAX_SAMPLES)
        row_projection = rgb_image.resize(
            (sample_width, height), Image.Resampling.NEAREST
        )
        col_projection = rgb_image.resize(
            (width, sample_height), Image.Resampling.NEAREST
        )
        row_rgb = np.asarray(row_projection, dtype=np.uint8).astype(np.uint16)
        col_rgb = np.asarray(col_projection, dtype=np.uint8).astype(np.uint16)
    except Exception as exc:
        raise PaletteAnalysisError(
            f"Letterbox detection could not analyze the source image: {exc}"
        ) from exc

    if row_rgb.shape != (height, sample_width, 3):
        raise PaletteAnalysisError(
            "Letterbox row projection is malformed: "
            f"shape {row_rgb.shape}, expected {(height, sample_width, 3)}"
        )
    if col_rgb.shape != (sample_height, width, 3):
        raise PaletteAnalysisError(
            "Letterbox column projection is malformed: "
            f"shape {col_rgb.shape}, expected {(sample_height, width, 3)}"
        )
    row_luma = (
        54 * row_rgb[..., 0] + 183 * row_rgb[..., 1] + 19 * row_rgb[..., 2]
    ) // 256
    col_luma = (
        54 * col_rgb[..., 0] + 183 * col_rgb[..., 1] + 19 * col_rgb[..., 2]
    ) // 256
    row_channel_spread = row_rgb.max(axis=2) - row_rgb.min(axis=2)
    col_channel_spread = col_rgb.max(axis=2) - col_rgb.min(axis=2)

    near_black_rows = (
        (row_luma <= _LETTERBOX_NEAR_BLACK_LUMA)
        & (row_channel_spread <= _LETTERBOX_MAX_CHANNEL_SPREAD)
    )
    near_black_cols = (
        (col_luma <= _LETTERBOX_NEAR_BLACK_LUMA)
        & (col_channel_spread <= _LETTERBOX_MAX_CHANNEL_SPREAD)
    )
    dark_rows = near_black_rows.mean(axis=1) >= _LETTERBOX_DARK_COVERAGE
    dark_cols = near_black_cols.mean(axis=0) >= _LETTERBOX_DARK_COVERAGE

    top_run = _edge_run_length(dark_rows)
    bottom_run = _edge_run_length(dark_rows[::-1])
    left_run = _edge_run_length(dark_cols)
    right_run = _edge_run_length(dark_cols[::-1])

    min_horizontal = max(
        _LETTERBOX_MIN_THICKNESS_PIXELS,
        int(np.ceil(height * _LETTERBOX_MIN_THICKNESS_FRACTION)),
    )
    min_vertical = max(
        _LETTERBOX_MIN_THICKNESS_PIXELS,
        int(np.ceil(width * _LETTERBOX_MIN_THICKNESS_FRACTION)),
    )
    top_detected = _edge_has_content_transition(
        row_luma, near_black_rows, top_run, min_horizontal
    )
    bottom_detected = _edge_has_content_transition(
        row_luma[::-1], near_black_rows[::-1], bottom_run, min_horizontal
    )
    left_detected = _edge_has_content_transition(
        col_luma.T, near_black_cols.T, left_run, min_vertical
    )
    right_detected = _edge_has_content_transition(
        col_luma.T[::-1], near_black_cols.T[::-1], right_run, min_vertical
    )

    top = top_run if top_detected else 0
    bottom = bottom_run if bottom_detected else 0
    left = left_run if left_detected else 0
    right = right_run if right_detected else 0

    # Overlapping detections are ambiguous evidence, so retain that full axis.
    if top + bottom >= height:
        top = bottom = 0
        top_detected = bottom_detected = False
    if left + right >= width:
        left = right = 0
        left_detected = right_detected = False

    detected_horizontal = top_detected or bottom_detected
    detected_vertical = left_detected or right_detected
    bbox = [left, top, width - right, height - bottom]
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise PaletteAnalysisError(
            f"Letterbox detection produced an invalid content bbox: {bbox}"
        )

    return {
        "bbox": bbox,
        "top": top,
        "bottom": bottom,
        "left": left,
        "right": right,
        "detected": detected_horizontal or detected_vertical,
        "detected_horizontal": detected_horizontal,
        "detected_vertical": detected_vertical,
    }


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


def get_thumbnail_palette_path(
    project_path: str,
    media_id: str,
    media_type: str,
) -> Path:
    """Return the canonical media-ID cache path for a thumbnail palette."""
    return (
        Path(project_path)
        / "data"
        / "palettes"
        / media_type
        / "thumbnail"
        / f"{media_id}.json"
    )


def load_thumbnail_palette(
    project_path: str,
    media_id: str,
    media_type: str,
) -> dict | None:
    """Load one cached thumbnail palette, or return ``None`` if unavailable."""
    path = get_thumbnail_palette_path(project_path, media_id, media_type)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data.get("source") == "thumbnail" else None
    except (OSError, json.JSONDecodeError):
        return None


def save_thumbnail_palette(
    project_path: str,
    media_id: str,
    media_type: str,
    data: dict,
    *,
    force: bool = False,
) -> Path:
    """Atomically persist one thumbnail palette under the existing cache root."""
    destination = get_thumbnail_palette_path(project_path, media_id, media_type)
    if destination.exists() and not force:
        raise FileExistsError(
            f"Thumbnail palette cache already exists: {destination}\n"
            "  Pass --force to overwrite."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    from data.annotate import atomic_write_text

    atomic_write_text(
        destination,
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return destination


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


# ---------------------------------------------------------------------------
# Colour extraction
# ---------------------------------------------------------------------------

# ===========================================================================
# Figure-ground colour analysis pipeline
# ===========================================================================
#
# Required analysis path
# ----------------------
# Configured palette segmentation + mean-shift simplification + agglomerative
# Ward clustering in CIELAB space. Any unavailable or failed stage aborts
# analysis.
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

    Requires ``cv2.pyrMeanShiftFiltering``; unavailable or failed processing is
    an explicit analysis error.

    Parameters
    ----------
    arr : (H, W, 3) uint8 RGB array.

    Returns
    -------
    (H, W, 3) uint8 RGB simplified array — same shape as input.
    """
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise PaletteAnalysisError(
            "OpenCV unavailable for palette mean-shift analysis"
        ) from exc
    try:
        return cv2.pyrMeanShiftFiltering(arr, sp=_FIG_MS_SP, sr=_FIG_MS_SR)
    except Exception as exc:
        raise PaletteAnalysisError(
            f"Palette mean-shift analysis failed: {exc}"
        ) from exc


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
        Candidate entries contain:
        ``rgb``, ``lab``, ``luminance``, ``chroma``, ``weight``, ``size``.
        Raises ``PaletteAnalysisError`` when Ward clustering cannot run.
    """
    if len(pixels_rgb) == 0:
        raise PaletteAnalysisError("Palette segmentation produced an empty region")

    try:
        from scipy.cluster.hierarchy import linkage, fcluster  # type: ignore
    except ImportError as exc:
        raise PaletteAnalysisError(
            "SciPy unavailable for palette Ward clustering"
        ) from exc

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
        raise PaletteAnalysisError(
            "Palette region has insufficient pixels for Ward clustering"
        )

    # --- Ward linkage on sample LAB values -----------------------------------
    try:
        Z = linkage(sample_lab.astype(np.float64), method="ward")
        sample_labels = fcluster(Z, t=k, criterion="maxclust")  # 1-indexed
    except Exception as exc:
        raise PaletteAnalysisError(
            f"Palette Ward clustering failed: {exc}"
        ) from exc

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
        raise PaletteAnalysisError("Palette Ward clustering produced no candidates")

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
        raise PaletteAnalysisError("Palette region produced no colour candidates")
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


def _segment_palette_masks(
    arr_rgb: np.ndarray,
    segmenter,
) -> "tuple[np.ndarray, np.ndarray, float]":
    """Classify pixels using the canonical palette segmentation interface.

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

    segment_palette = getattr(segmenter, "segment_palette", None)
    if not callable(segment_palette):
        raise PaletteAnalysisError(
            "Palette segmentation unavailable: configured model does not expose "
            "segment_palette(image_pil)"
        )
    try:
        from PIL import Image

        masks = segment_palette(Image.fromarray(arr_rgb, "RGB"))
    except Exception as exc:
        raise PaletteAnalysisError(
            f"Palette segmentation failed: {exc}"
        ) from exc
    if not isinstance(masks, (list, tuple)):
        raise PaletteAnalysisError(
            "Palette segmentation returned a malformed mask collection"
        )

    cy_lo, cy_hi = int(h * 0.20), int(h * 0.80)
    cx_lo, cx_hi = int(w * 0.20), int(w * 0.80)

    for m in masks:
        if not isinstance(m, dict) or "segmentation" not in m:
            raise PaletteAnalysisError(
                "Palette segmentation returned a malformed mask entry"
            )
        seg = np.asarray(m["segmentation"], dtype=bool)
        if seg.shape != (h, w):
            raise PaletteAnalysisError(
                "Palette segmentation returned mask shape "
                f"{seg.shape}; expected {(h, w)}"
            )
        if not check_containment(seg):
            continue
        bbox = m.get("bbox", [])
        if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            raise PaletteAnalysisError(
                "Palette segmentation returned a mask without a valid bbox"
            )
        bx, by, bw_m, bh_m = bbox[:4]
        cx_centre = bx + bw_m / 2
        cy_centre = by + bh_m / 2
        if not (cx_lo <= cx_centre <= cx_hi and cy_lo <= cy_centre <= cy_hi):
            continue
        fg_mask |= seg
        bg_mask &= ~seg

    confidence = round(float(fg_mask.sum()) / float(h * w), 4)
    if fg_mask.sum() < h * w * 0.02:
        raise PaletteAnalysisError(
            "Palette segmentation returned no usable foreground segmentation"
        )
    if not bg_mask.any():
        raise PaletteAnalysisError(
            "Palette segmentation returned no usable background region"
        )
    return fg_mask, bg_mask, confidence


def _extract_fg_bg_figure(
    arr_rgb: np.ndarray,
    *,
    segmenter=None,
) -> "tuple[dict, dict, dict]":
    """Full figure-ground extraction pipeline.

    Parameters
    ----------
    arr_rgb         : (H, W, 3) uint8 RGB array (already resized).
    segmenter : Configured adapter implementing ``segment_palette(image_pil)``.

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

    # Step 2 — required semantic segmentation
    if segmenter is None:
        raise PaletteAnalysisError(
            "Palette segmentation unavailable: no configured model loaded"
        )
    fg_mask, bg_mask, segmentation_confidence = _segment_palette_masks(
        arr_rgb, segmenter
    )

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
        "segmentation_used":       getattr(
            segmenter, "model_name", type(segmenter).__name__
        ),
        "segmentation_confidence": segmentation_confidence,
        "superpixels_used":        superpixels_used,
        "fg_region_count":         int(fg_mask.sum()),
        "bg_region_count":         int(bg_mask.sum()),
        "cluster_count":           _FIG_N_CLUSTERS,
        "fallback_level":          0,
    }
    return fg_out, bg_out, diagnostics


def _extract_fg_bg_full(
    image_path: Path,
    *,
    segmenter=None,
) -> "tuple[dict, dict, dict]":
    """Full figure-ground extraction returning ``(foreground, background, diagnostics)``.

    Parameters
    ----------
    image_path        : Path to a PNG or other Pillow-readable image.
    segmenter: Configured adapter implementing ``segment_palette(image_pil)``.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow is required for palette extraction.  "
            "Install with:  pip install Pillow"
        ) from exc

    try:
        with Image.open(image_path) as opened:
            source_image = opened.convert("RGB")
        source_width, source_height = source_image.size
        content = _detect_content_bbox(source_image)
        active_image = source_image.crop(tuple(content["bbox"]))
        arr = np.asarray(
            active_image.resize((_FIG_RESIZE, _FIG_RESIZE), Image.LANCZOS)
        )
    except PaletteAnalysisError:
        raise
    except Exception as exc:
        raise PaletteAnalysisError(
            f"Palette source image could not be analyzed: {exc}"
        ) from exc

    foreground, background, diagnostics = _extract_fg_bg_figure(
        arr, segmenter=segmenter
    )
    diagnostics["source_image"] = {
        "width": source_width,
        "height": source_height,
        "content_bbox": {
            "left": content["bbox"][0],
            "top": content["bbox"][1],
            "right": content["bbox"][2],
            "bottom": content["bbox"][3],
        },
        "letterbox": {
            "detected": content["detected"],
            "top": content["top"],
            "bottom": content["bottom"],
            "left": content["left"],
            "right": content["right"],
            "detected_horizontal": content["detected_horizontal"],
            "detected_vertical": content["detected_vertical"],
        },
    }
    return foreground, background, diagnostics


def extract_fg_bg(
    image_path: Path,
    *,
    segmenter=None,
) -> "tuple[dict, dict]":
    """Extract dominant foreground and background colours from a frame image.

    Parameters
    ----------
    image_path        : Path to a PNG (or any Pillow-readable) image.
    segmenter: Configured adapter implementing ``segment_palette(image_pil)``.

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
        image_path, segmenter=segmenter
    )
    return fg, bg


def _load_palette_segmenter(project_path: str, verbose: bool = False):
    """Load the configured model's canonical palette segmenter."""
    from tool import prefs as _prefs

    sam_name = _prefs.get("model_segmentation")
    if not sam_name:
        raise PaletteAnalysisError(
            "Palette segmentation unavailable: no model configured"
        )
    try:
        from services.silhouette import load_sam_model  # type: ignore

        segmenter, _, device = load_sam_model(project_path, sam_name)
    except Exception as exc:
        raise PaletteAnalysisError(
            f"Palette segmentation unavailable: {exc}"
        ) from exc
    if not callable(getattr(segmenter, "segment_palette", None)):
        raise PaletteAnalysisError(
            "Palette segmentation unavailable: configured model does not expose "
            "segment_palette(image_pil)"
        )
    if verbose:
        print(f"  Segmentation model loaded: {sam_name} ({device})")
    return segmenter


def _thumbnail_source_fingerprint(project_path: str, source_path: Path) -> dict:
    """Return a persistent content fingerprint for thumbnail cache freshness."""
    stat = source_path.stat()
    try:
        display_path = str(source_path.relative_to(project_path))
    except ValueError:
        display_path = str(source_path)
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    return {
        "path": display_path,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest,
    }


def _thumbnail_cache_is_current(cache: dict, source_image: dict) -> bool:
    cached_source = cache.get("source_image", {})
    return (
        cached_source.get("path") == source_image.get("path")
        and cached_source.get("sha256") == source_image.get("sha256")
    )


def create_thumbnail_palette(
    project_path: str,
    filename: str,
    media_type: str = "movie",
    *,
    metadata: dict | None = None,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Extract and cache foreground/background colours from a metadata thumbnail."""
    if metadata is None:
        entries = get_metadata(project_path, media_type=media_type)
        metadata = next(
            (entry for entry in entries if entry.get("filename") == filename),
            {"filename": filename},
        )

    media_id = str(
        metadata.get("media_id") or compute_media_id(metadata, media_type)
    )
    source_path = resolve_thumbnail_path(project_path, media_type, filename)
    if source_path is None:
        raise FileNotFoundError(
            f"Metadata thumbnail not found for '{filename}' ({media_type})"
        )

    source_image = _thumbnail_source_fingerprint(project_path, source_path)
    cache_path = get_thumbnail_palette_path(project_path, media_id, media_type)
    existing = load_thumbnail_palette(project_path, media_id, media_type)
    if existing is not None and not force:
        if _thumbnail_cache_is_current(existing, source_image):
            if verbose:
                print(f"  skip  {filename}: thumbnail palette already cached")
            return {
                "filename": filename,
                "media_id": media_id,
                "processed": 0,
                "skipped": 0,
                "failed": 0,
                "cached": True,
            }
        if verbose:
            print(f"  stale {filename}: thumbnail image changed — regenerating")

    foreground, background, diagnostics = _extract_fg_bg_full(
        source_path,
        segmenter=_load_palette_segmenter(project_path, verbose),
    )
    identity = {
        "media_id": media_id,
        "media_type": media_type,
        "filename": filename,
        "title": metadata.get("title") or Path(filename).stem,
        "year": metadata.get("year"),
        "tmdb": metadata.get("tmdb") or metadata.get("tmdb_id"),
    }
    document = {
        "schema_version": 1,
        "media": identity,
        "source": "thumbnail",
        "source_image": source_image,
        "method": "figure",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "thumbnail": {
            "foreground": foreground,
            "background": background,
            "diagnostics": diagnostics,
        },
    }
    save_thumbnail_palette(
        project_path,
        media_id,
        media_type,
        document,
        force=cache_path.exists() or force,
    )
    if verbose:
        print(f"  → saved  {cache_path.relative_to(project_path)}")
    return {
        "filename": filename,
        "media_id": media_id,
        "processed": 1,
        "skipped": 0,
        "failed": 0,
        "cached": False,
    }


def create_thumbnail_palettes_for_all(
    project_path: str,
    media_type: str = "movie",
    *,
    force: bool = False,
    verbose: bool = False,
    on_item_done=None,
) -> dict:
    """Build thumbnail palettes from one loaded metadata collection."""
    entries = get_metadata(project_path, media_type=media_type)
    total_processed = 0
    total_skipped = 0
    total_failed = 0
    total_cached = 0
    results = []

    for metadata in entries:
        filename = metadata.get("filename")
        if not filename:
            continue
        try:
            summary = create_thumbnail_palette(
                project_path,
                filename,
                media_type,
                metadata=metadata,
                force=force,
                verbose=verbose,
            )
        except FileNotFoundError as exc:
            print(f"  skip  {filename}: {exc}", flush=True)
            total_skipped += 1
            summary = {"filename": filename, "skipped": True, "reason": str(exc)}
            if on_item_done is not None:
                on_item_done(filename, None, exc)
        except Exception as exc:
            print(f"  fail  {filename}: {exc}", flush=True)
            total_failed += 1
            summary = {"filename": filename, "error": str(exc)}
            if on_item_done is not None:
                on_item_done(filename, None, exc)
        else:
            if summary.get("cached"):
                total_cached += 1
            else:
                total_processed += summary.get("processed", 0)
            if on_item_done is not None:
                on_item_done(filename, summary, None)
        results.append(summary)

    return {
        "media_type": media_type,
        "total_files": len(results),
        "total_processed": total_processed,
        "total_skipped": total_skipped,
        "total_failed": total_failed,
        "total_cached": total_cached,
        "results": results,
    }


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
    segmenter=None,
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
            png_path, segmenter=segmenter
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
    media_type: str = "movie",
    *,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Build and cache a palette for every shot in *filename*.

    Reads the annotation JSON and shotlist; for each shot that has a
    ``best_frame`` PNG, extracts foreground and background colours using
    the figure-ground pipeline.

    The segmentation model is loaded automatically from the project's
    ``model_segmentation`` preference
    (``crossing tool model set segmentation <name>``).
    Missing, incompatible, failed, or unusable segmentation is an explicit
    analysis failure and no palette cache is written.

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
    failure_reasons: list[str] = []

    # Load the canonical palette segmenter once for the whole movie.
    segmenter = _load_palette_segmenter(project_path, verbose)

    for i, entry in enumerate(entries):
        shot_data = entry.get("shot", {})
        shot_id = str(shot_data.get("shot_id", ""))
        shot_info = shots_by_id.get(shot_id)

        result = _process_one_shot(
            project_path, filename, media_type, entry, i, shot_info,
            segmenter=segmenter,
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
            failure_reasons.append(f"shot {i}: {reason}")
            print(f"  fail  {filename} shot {i}: {reason}", flush=True)

        shot_palettes.append(result)

    summary = {
        "shot_count": len(entries),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
    }

    if failed:
        raise PaletteAnalysisError(
            f"Figure-ground extraction failed for '{filename}' "
            f"({failed} shot(s)): {failure_reasons[0]}"
        )

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
    media_type: str = "movie",
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
            total_failed += 1
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
