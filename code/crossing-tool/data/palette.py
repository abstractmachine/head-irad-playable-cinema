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

Algorithm — semantic figure
---------------------------
1. Detect paired, continuous near-black bars at the original image edges.
2. Crop to the detected active-image bounds, or retain the full image when no
    bars are detected.
3. Resize the active image to 256×256 (LANCZOS) for texture detail.
4. Read ``humans``, ``animals``, and ``objects`` from the image's canonical
    shot annotation, then request each concept through the configured model's
    ``segment_concept(image_pil, concept)`` interface. All populated categories
    contribute in that priority order. If they are empty, segment ``setting``
    in explicit setting mode; if all four fields are empty, fail.
5. Segment the working image into deterministic OpenCV SEEDS superpixels,
    majority-assign each superpixel to foreground or background, and calculate
    one assigned-region RGB mean per superpixel while retaining its pixel area.
6. Cluster each region's area-weighted superpixel means with agglomerative Ward
    clustering in CIELAB space using scipy.
7. Rank clusters by perceptual and spatial-area weight; apply a rescue pass when both
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
Required for extraction: OpenCV contrib's ximgproc SEEDS implementation, scipy,
and a configured segmentation adapter exposing
``segment_concept(image_pil, concept)``.
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
_FIG_N_CLUSTERS = 8     # agglomerative clusters per region
_FIG_N_PALETTE  = 4     # palette entries kept per region

PALETTE_SCHEMA_VERSION = 2
PALETTE_ANALYSIS_VERSION = "semantic-v1"

# VIAN-style colour reduction: SEEDS superpixels become Ward representatives.
_SEEDS_TARGET_SUPERPIXELS = 1024
_SEEDS_NUM_LEVELS = 4
_SEEDS_PRIOR = 2
_SEEDS_HISTOGRAM_BINS = 5
_SEEDS_DOUBLE_STEP = False
_SEEDS_ITERATIONS = 4

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


def _perceptual_weights(
    lab: np.ndarray,
    pixel_counts: np.ndarray | None = None,
) -> np.ndarray:
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

    if pixel_counts is None:
        pixel_counts = np.ones(len(lab), dtype=np.float64)
    else:
        pixel_counts = np.asarray(pixel_counts, dtype=np.float64)
        if pixel_counts.shape != (len(lab),) or np.any(pixel_counts <= 0):
            raise PaletteAnalysisError(
                "Palette representative pixel counts are malformed"
            )
    dark_frac = float(
        pixel_counts[L < _LAB_MIN_L].sum() / pixel_counts.sum()
    )
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
# Configured palette segmentation + SEEDS superpixel reduction + agglomerative
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


def _extract_seeds_superpixels(
    arr_rgb: np.ndarray,
    fg_mask: np.ndarray,
    bg_mask: np.ndarray,
    *,
    diagnostic_capture: dict | None = None,
) -> "tuple[list[dict], list[dict], dict]":
    """Reduce an RGB image to region-assigned OpenCV SEEDS representatives.

    SEEDS runs once on the complete working image. Each spatially coherent
    superpixel is assigned to foreground only when a strict majority of its
    pixels belong to the SAM foreground mask; background wins ties. For a
    boundary-crossing label, its RGB representative is averaged only from
    pixels in the assigned region, avoiding cross-region colour contamination,
    while ``pixel_count`` retains the label's complete spatial area.
    """
    if (
        arr_rgb.ndim != 3
        or arr_rgb.shape[2] != 3
        or arr_rgb.dtype != np.uint8
    ):
        raise PaletteAnalysisError(
            "SEEDS requires an H×W uint8 RGB working image"
        )
    height, width = arr_rgb.shape[:2]
    expected_mask_shape = (height, width)
    fg_mask = np.asarray(fg_mask, dtype=bool)
    bg_mask = np.asarray(bg_mask, dtype=bool)
    if fg_mask.shape != expected_mask_shape or bg_mask.shape != expected_mask_shape:
        raise PaletteAnalysisError(
            "SEEDS region masks do not match the working image"
        )
    if np.any(fg_mask & bg_mask) or not np.all(fg_mask | bg_mask):
        raise PaletteAnalysisError(
            "SEEDS region masks must be disjoint and cover the working image"
        )

    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise PaletteAnalysisError(
            "OpenCV contrib SEEDS unavailable: install "
            "opencv-contrib-python-headless>=4.10"
        ) from exc
    factory = getattr(
        getattr(cv2, "ximgproc", None), "createSuperpixelSEEDS", None
    )
    if not callable(factory):
        raise PaletteAnalysisError(
            "OpenCV contrib SEEDS unavailable: cv2.ximgproc."
            "createSuperpixelSEEDS is missing"
        )

    try:
        seeds = factory(
            width,
            height,
            3,
            _SEEDS_TARGET_SUPERPIXELS,
            _SEEDS_NUM_LEVELS,
            _SEEDS_PRIOR,
            _SEEDS_HISTOGRAM_BINS,
            _SEEDS_DOUBLE_STEP,
        )
        bgr = np.ascontiguousarray(arr_rgb[:, :, ::-1])
        seeds.iterate(bgr, _SEEDS_ITERATIONS)
        labels = np.asarray(seeds.getLabels())
    except Exception as exc:
        raise PaletteAnalysisError(
            f"SEEDS superpixel segmentation failed: {exc}"
        ) from exc

    if labels.shape != expected_mask_shape:
        raise PaletteAnalysisError(
            "SEEDS returned malformed labels: "
            f"shape {labels.shape}, expected {expected_mask_shape}"
        )
    label_ids = np.unique(labels)
    if len(label_ids) == 0:
        raise PaletteAnalysisError("SEEDS produced no superpixels")
    if len(label_ids) > _SEEDS_TARGET_SUPERPIXELS:
        raise PaletteAnalysisError(
            f"SEEDS produced {len(label_ids)} superpixels, exceeding the "
            f"configured maximum of {_SEEDS_TARGET_SUPERPIXELS}"
        )

    foreground: list[dict] = []
    background: list[dict] = []
    areas: list[int] = []
    boundary_crossing = 0
    diagnostic_regions: list[dict] = []
    for label_id in label_ids:
        label_mask = labels == label_id
        pixel_count = int(label_mask.sum())
        fg_count = int(np.count_nonzero(label_mask & fg_mask))
        bg_count = pixel_count - fg_count
        if fg_count and bg_count:
            boundary_crossing += 1

        if fg_count > bg_count:
            region_mask = label_mask & fg_mask
            destination = foreground
            region = "foreground"
        else:
            region_mask = label_mask & bg_mask
            destination = background
            region = "background"

        diagnostic_regions.append({
            "label": int(label_id),
            "assignment": region,
            "foreground_pixel_count": fg_count,
            "background_pixel_count": bg_count,
            "boundary_crossing": bool(fg_count and bg_count),
            "tie": bool(fg_count and fg_count == bg_count),
        })

        region_pixels = arr_rgb[region_mask]
        if len(region_pixels) == 0:
            raise PaletteAnalysisError(
                f"SEEDS superpixel {int(label_id)} has no {region} pixels"
            )
        mean_rgb = np.rint(
            region_pixels.astype(np.float64).mean(axis=0)
        ).astype(np.uint8)
        destination.append({
            "label": int(label_id),
            "rgb": mean_rgb.tolist(),
            "pixel_count": pixel_count,
            "region_pixel_count": int(len(region_pixels)),
        })
        areas.append(pixel_count)

    if not foreground:
        raise PaletteAnalysisError(
            "SEEDS produced no foreground superpixels after majority assignment"
        )
    if not background:
        raise PaletteAnalysisError(
            "SEEDS produced no background superpixels after majority assignment"
        )

    if diagnostic_capture is not None:
        diagnostic_capture["seeds_labels"] = labels.copy()
        diagnostic_capture["seeds_regions"] = diagnostic_regions
        diagnostic_capture["seeds_superpixels"] = {
            "foreground": [dict(item) for item in foreground],
            "background": [dict(item) for item in background],
        }

    return foreground, background, {
        "superpixels_used": True,
        "superpixel_count": len(label_ids),
        "foreground_superpixel_count": len(foreground),
        "background_superpixel_count": len(background),
        "boundary_crossing_superpixel_count": boundary_crossing,
        "mean_superpixel_area": round(float(np.mean(areas)), 2),
        "min_superpixel_area": min(areas),
        "max_superpixel_area": max(areas),
        "superpixel_region_rule": "strict_foreground_majority_background_tie",
    }


def _agglomerative_palette(
    superpixels: list[dict],
    n_clusters: int = _FIG_N_CLUSTERS,
    n_palette: int = _FIG_N_PALETTE,
    *,
    diagnostic_capture: dict | None = None,
) -> "list[dict]":
    """Agglomerative Ward clustering of area-weighted SEEDS means in LAB.

    More perceptually stable than k-means: bottom-up merging avoids local-
    minima sensitivity and never requires a random seed.

    Algorithm
    ---------
    1. Convert each superpixel-average RGB representative to CIELAB.
    2. Run Ward linkage over those representatives.
    3. Compute area-weighted RGB/LAB centroids for the resulting clusters.
    4. Rank clusters by perceptual weight multiplied by represented pixel area.

    Returns
    -------
    list[dict]
        Candidate entries contain:
        ``rgb``, ``lab``, ``luminance``, ``chroma``, ``weight``, ``size``.
        Raises ``PaletteAnalysisError`` when Ward clustering cannot run.
    """
    if not superpixels:
        raise PaletteAnalysisError("Palette superpixels produced an empty region")

    try:
        representative_rgb = np.asarray(
            [item["rgb"] for item in superpixels], dtype=np.uint8
        )
        pixel_counts = np.asarray(
            [item["pixel_count"] for item in superpixels], dtype=np.int64
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PaletteAnalysisError(
            f"Palette superpixel representatives are malformed: {exc}"
        ) from exc
    if representative_rgb.shape != (len(superpixels), 3):
        raise PaletteAnalysisError(
            "Palette superpixel RGB representatives are malformed"
        )
    if pixel_counts.shape != (len(superpixels),) or np.any(pixel_counts <= 0):
        raise PaletteAnalysisError(
            "Palette superpixel pixel counts are malformed"
        )

    try:
        from scipy.cluster.hierarchy import linkage, fcluster  # type: ignore
    except ImportError as exc:
        raise PaletteAnalysisError(
            "SciPy unavailable for palette Ward clustering"
        ) from exc

    representative_lab = _rgb_to_lab(representative_rgb)
    k = min(n_clusters, len(superpixels))
    if k < 2:
        raise PaletteAnalysisError(
            "Palette region has insufficient superpixels for Ward clustering"
        )

    # Ward determines colour-space groups; area weights determine their
    # representative centroids and perceptual ranking below.
    try:
        linkage_matrix = linkage(
            representative_lab.astype(np.float64), method="ward"
        )
        cluster_labels = fcluster(
            linkage_matrix, t=k, criterion="maxclust"
        )
    except Exception as exc:
        raise PaletteAnalysisError(
            f"Palette Ward clustering failed: {exc}"
        ) from exc

    weights = _perceptual_weights(representative_lab, pixel_counts)

    candidates: list[dict] = []
    diagnostic_clusters: list[dict] = []
    for cluster_id in np.unique(cluster_labels):
        cluster_mask = cluster_labels == cluster_id
        cluster_areas = pixel_counts[cluster_mask].astype(np.float64)
        count = int(cluster_areas.sum())
        cluster_weight = float(
            np.sum(weights[cluster_mask] * cluster_areas)
        )
        mean_rgb = np.average(
            representative_rgb[cluster_mask].astype(np.float64),
            axis=0,
            weights=cluster_areas,
        )
        mean_lab = np.average(
            representative_lab[cluster_mask].astype(np.float64),
            axis=0,
            weights=cluster_areas,
        )
        chroma = float(np.sqrt(float(mean_lab[1]) ** 2 + float(mean_lab[2]) ** 2))
        candidate = {
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
        }
        candidates.append(candidate)
        diagnostic_clusters.append({
            "cluster_id": int(cluster_id),
            "rgb": list(candidate["rgb"]),
            "lab": list(candidate["lab"]),
            "luminance": candidate["luminance"],
            "chroma": candidate["chroma"],
            "weight": candidate["weight"],
            "size": candidate["size"],
            "superpixel_count": int(np.count_nonzero(cluster_mask)),
        })

    if not candidates:
        raise PaletteAnalysisError("Palette Ward clustering produced no candidates")

    candidates.sort(key=lambda c: c["weight"], reverse=True)
    if diagnostic_capture is not None:
        diagnostic_clusters.sort(key=lambda item: item["weight"], reverse=True)
        diagnostic_capture.update({
            "superpixel_labels": [int(item["label"]) for item in superpixels],
            "cluster_labels": [int(value) for value in cluster_labels],
            "clusters": diagnostic_clusters,
            "ranked_candidates": diagnostic_clusters[:n_palette],
        })
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


_SEMANTIC_CATEGORY_PRIORITY = ("humans", "animals", "objects")


def _semantic_selection(annotation: dict | None) -> "tuple[str, list[tuple[str, list[str]]]]":
    """Return the intentional semantic mode and ordered annotation concepts."""
    annotation = annotation if isinstance(annotation, dict) else {}
    selected: list[tuple[str, list[str]]] = []
    seen: set[str] = set()

    for category in _SEMANTIC_CATEGORY_PRIORITY:
        value = annotation.get(category, [])
        if value is None:
            value = []
        if not isinstance(value, (list, tuple)):
            raise PaletteAnalysisError(
                f"Palette annotation field '{category}' must be a list"
            )
        concepts: list[str] = []
        for raw_concept in value:
            concept = str(raw_concept).strip()
            key = concept.casefold()
            if not concept or key in seen:
                continue
            seen.add(key)
            concepts.append(concept)
        if concepts:
            selected.append((category, concepts))

    if selected:
        return "semantic", selected

    setting = annotation.get("setting", "")
    if setting is None:
        setting = ""
    if not isinstance(setting, str):
        raise PaletteAnalysisError(
            "Palette annotation field 'setting' must be a string"
        )
    setting = setting.strip()
    if setting:
        return "setting", [("setting", [setting])]

    raise PaletteAnalysisError(
        "No semantic image categories available for palette extraction."
    )


def _select_semantic_foreground(
    arr_rgb: np.ndarray,
    *,
    annotation: dict | None,
    segmenter,
) -> "tuple[np.ndarray, np.ndarray, dict]":
    """Build a deterministic semantic foreground mask from shot annotation.

    Every non-empty humans/animals/objects category contributes concepts in
    priority order. Overlap pixels belong to the first category in that order.
    When those categories are empty, the setting string is segmented as an
    intentional setting mode. No spatial or colour-derived region is created.

    Returns
    -------
    (fg_mask, bg_mask, diagnostics)
        fg_mask, bg_mask — (H, W) bool arrays.
        diagnostics      — semantic mode, concepts, mask count, and coverage.
    """
    h, w = arr_rgb.shape[:2]
    fg_mask = np.zeros((h, w), dtype=bool)
    segment_concept = getattr(segmenter, "segment_concept", None)
    if not callable(segment_concept):
        raise PaletteAnalysisError(
            "Palette segmentation unavailable: configured model does not expose "
            "segment_concept(image_pil, concept)"
        )
    foreground_mode, category_concepts = _semantic_selection(annotation)

    from PIL import Image

    image_pil = Image.fromarray(arr_rgb, "RGB")
    semantic_mask_count = 0
    category_pixel_counts: dict[str, int] = {}
    concepts_used: list[str] = []
    for category, concepts in category_concepts:
        category_mask = np.zeros((h, w), dtype=bool)
        concepts_used.extend(concepts)
        for concept in concepts:
            try:
                masks = segment_concept(image_pil, concept)
            except Exception as exc:
                raise PaletteAnalysisError(
                    f"Semantic palette segmentation failed for '{concept}': {exc}"
                ) from exc
            if not isinstance(masks, (list, tuple)):
                raise PaletteAnalysisError(
                    f"Semantic palette segmentation for '{concept}' returned "
                    "a malformed mask collection"
                )
            for mask in masks:
                if not isinstance(mask, dict) or "segmentation" not in mask:
                    raise PaletteAnalysisError(
                        f"Semantic palette segmentation for '{concept}' returned "
                        "a malformed mask entry"
                    )
                segmentation = np.asarray(mask["segmentation"], dtype=bool)
                if segmentation.shape != (h, w):
                    raise PaletteAnalysisError(
                        f"Semantic palette segmentation for '{concept}' returned "
                        f"mask shape {segmentation.shape}; expected {(h, w)}"
                    )
                if not segmentation.any():
                    continue
                category_mask |= segmentation
                semantic_mask_count += 1

        exclusive_category_mask = category_mask & ~fg_mask
        category_pixel_counts[category] = int(exclusive_category_mask.sum())
        fg_mask |= category_mask

    bg_mask = ~fg_mask
    semantic_coverage = round(float(fg_mask.sum()) / float(h * w), 4)
    if fg_mask.sum() < h * w * 0.02:
        raise PaletteAnalysisError(
            "Semantic palette segmentation returned no usable masks for "
            f"concepts: {', '.join(concepts_used)}"
        )
    if not bg_mask.any():
        raise PaletteAnalysisError(
            "Semantic palette segmentation returned no usable background region"
        )
    return fg_mask, bg_mask, {
        "foreground_mode": foreground_mode,
        "semantic_categories_used": [
            category for category, _concepts in category_concepts
        ],
        "semantic_concepts_used": concepts_used,
        "semantic_mask_count": semantic_mask_count,
        "semantic_foreground_coverage": semantic_coverage,
        "semantic_category_pixel_counts": category_pixel_counts,
    }


def _extract_fg_bg_figure(
    arr_rgb: np.ndarray,
    *,
    annotation: dict | None = None,
    segmenter=None,
    diagnostic_capture: dict | None = None,
) -> "tuple[dict, dict, dict]":
    """Full figure-ground extraction pipeline.

    Parameters
    ----------
    arr_rgb         : (H, W, 3) uint8 RGB array (already resized).
    segmenter : Configured adapter implementing
        ``segment_concept(image_pil, concept)``.

    Returns
    -------
    (foreground_dict, background_dict, diagnostics_dict)
        foreground / background have the extended schema with ``"palette"``
        and ``"coverage"`` in addition to the standard keys.
        diagnostics_dict carries all rescue-pass keys plus
        ``"method_used"``, ``"segmentation_used"``,
        ``"segmentation_confidence"``, ``"superpixels_used"``,
        ``"fg_region_count"``, ``"bg_region_count"``,
        ``"cluster_count"``, and semantic category/mask diagnostics.
    """
    h, w = arr_rgb.shape[:2]
    total_pixels = h * w

    # Step 1 — required semantic segmentation
    if segmenter is None:
        raise PaletteAnalysisError(
            "Palette segmentation unavailable: no configured model loaded"
        )
    fg_mask, bg_mask, semantic_diagnostics = _select_semantic_foreground(
        arr_rgb, annotation=annotation, segmenter=segmenter
    )
    if diagnostic_capture is not None:
        diagnostic_capture["semantic_foreground_mask"] = fg_mask.copy()

    # Step 2 — VIAN-style SEEDS colour representatives, assigned by SAM region
    seeds_kwargs = {}
    if diagnostic_capture is not None:
        seeds_kwargs["diagnostic_capture"] = diagnostic_capture
    fg_superpixels, bg_superpixels, superpixel_diagnostics = (
        _extract_seeds_superpixels(
            arr_rgb, fg_mask, bg_mask, **seeds_kwargs
        )
    )

    # Step 3 — agglomerative clustering per region
    foreground_ward_capture = {} if diagnostic_capture is not None else None
    background_ward_capture = {} if diagnostic_capture is not None else None
    foreground_ward_kwargs = {}
    background_ward_kwargs = {}
    if foreground_ward_capture is not None:
        foreground_ward_kwargs["diagnostic_capture"] = foreground_ward_capture
        background_ward_kwargs["diagnostic_capture"] = background_ward_capture
    fg_candidates = _agglomerative_palette(
        fg_superpixels,
        _FIG_N_CLUSTERS,
        _FIG_N_PALETTE,
        **foreground_ward_kwargs,
    )
    bg_candidates = _agglomerative_palette(
        bg_superpixels,
        _FIG_N_CLUSTERS,
        _FIG_N_PALETTE,
        **background_ward_kwargs,
    )
    if diagnostic_capture is not None:
        diagnostic_capture["ward"] = {
            "foreground": foreground_ward_capture,
            "background": background_ward_capture,
        }

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
        "method_used":             "semantic-figure",
        "segmentation_used":       getattr(
            segmenter, "model_name", type(segmenter).__name__
        ),
        "segmentation_confidence": semantic_diagnostics[
            "semantic_foreground_coverage"
        ],
        **semantic_diagnostics,
        **superpixel_diagnostics,
        "fg_region_count":         int(fg_mask.sum()),
        "bg_region_count":         int(bg_mask.sum()),
        "cluster_count":           _FIG_N_CLUSTERS,
    }
    return fg_out, bg_out, diagnostics


def _extract_fg_bg_full(
    image_path: Path,
    *,
    annotation: dict | None = None,
    segmenter=None,
    diagnostic_capture: dict | None = None,
) -> "tuple[dict, dict, dict]":
    """Full figure-ground extraction returning ``(foreground, background, diagnostics)``.

    Parameters
    ----------
    image_path        : Path to a PNG or other Pillow-readable image.
    segmenter: Configured adapter implementing
        ``segment_concept(image_pil, concept)``.
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
        if diagnostic_capture is not None:
            diagnostic_capture["source_image"] = source_image.copy()
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

    figure_kwargs = {
        "annotation": annotation,
        "segmenter": segmenter,
    }
    if diagnostic_capture is not None:
        figure_kwargs["diagnostic_capture"] = diagnostic_capture
    foreground, background, diagnostics = _extract_fg_bg_figure(
        arr, **figure_kwargs
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
    annotation: dict | None = None,
    segmenter=None,
) -> "tuple[dict, dict]":
    """Extract dominant foreground and background colours from a frame image.

    Parameters
    ----------
    image_path        : Path to a PNG (or any Pillow-readable) image.
    segmenter: Configured adapter implementing
        ``segment_concept(image_pil, concept)``.

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
        image_path, annotation=annotation, segmenter=segmenter
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
    if not callable(getattr(segmenter, "segment_concept", None)):
        raise PaletteAnalysisError(
            "Palette segmentation unavailable: configured model does not expose "
            "segment_concept(image_pil, concept)"
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


def _semantic_input_fingerprint(annotation: dict | None) -> dict:
    """Return the normalized semantic input and its deterministic digest."""
    foreground_mode, category_concepts = _semantic_selection(annotation)
    semantic_input = {
        "foreground_mode": foreground_mode,
        "categories": [
            {"category": category, "concepts": concepts}
            for category, concepts in category_concepts
        ],
    }
    encoded = json.dumps(
        semantic_input, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        **semantic_input,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _thumbnail_cache_is_current(
    cache: dict,
    source_image: dict,
    semantic_input: dict,
) -> bool:
    cached_source = cache.get("source_image", {})
    cached_semantic_input = cache.get("semantic_input", {})
    return (
        cache.get("schema_version") == PALETTE_SCHEMA_VERSION
        and cache.get("analysis_version") == PALETTE_ANALYSIS_VERSION
        and cache.get("method") == "semantic-figure"
        and cached_source.get("path") == source_image.get("path")
        and cached_source.get("sha256") == source_image.get("sha256")
        and cached_semantic_input.get("sha256") == semantic_input.get("sha256")
    )


def _semantic_cache_is_current(cache: dict | None) -> bool:
    return bool(
        cache
        and cache.get("schema_version") == PALETTE_SCHEMA_VERSION
        and cache.get("analysis_version") == PALETTE_ANALYSIS_VERSION
        and cache.get("method") == "semantic-figure"
    )


def _timecode_seconds(value: str) -> float:
    try:
        hours, minutes, seconds = str(value).split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError) as exc:
        raise PaletteAnalysisError(
            f"Invalid shotlist timecode for thumbnail annotation: {value!r}"
        ) from exc


def _resolve_thumbnail_annotation(
    project_path: str,
    filename: str,
    media_type: str,
    media_id: str,
    metadata: dict,
) -> dict:
    """Resolve the shot annotation describing a canonical gameplay thumbnail."""
    if media_type != "gameplay":
        raise PaletteAnalysisError(
            "No semantic image categories available for palette extraction. "
            "The metadata thumbnail has no per-image shot annotation."
        )

    video_path = (
        Path(project_path) / "media" / "videos" / media_type / filename
    )
    if not video_path.exists():
        raise PaletteAnalysisError(
            "No semantic image categories available for palette extraction. "
            f"Thumbnail source video not found: {video_path}"
        )

    from services.transcode import get_video_thumbnail_timestamp

    target_seconds = get_video_thumbnail_timestamp(video_path)
    try:
        shotlist = read_shotlist(project_path, filename, media_type)
        entries = load_annotation_items(project_path, filename, media_type)
    except FileNotFoundError as exc:
        raise PaletteAnalysisError(
            "No semantic image categories available for palette extraction. "
            f"{exc}"
        ) from exc

    matching_shot_id = None
    for shot in shotlist:
        start_seconds = _timecode_seconds(shot.get("start_time", ""))
        end_seconds = _timecode_seconds(shot.get("end_time", ""))
        if start_seconds <= target_seconds <= end_seconds:
            matching_shot_id = build_shot_id(
                media_id,
                int(shot.get("start_frame") or 0),
                int(shot.get("end_frame") or 0),
            )
            break

    if matching_shot_id is None:
        raise PaletteAnalysisError(
            "No semantic image categories available for palette extraction. "
            f"No shot contains thumbnail timestamp {target_seconds:.3f}s."
        )

    for entry in entries:
        shot_data = entry.get("shot", {})
        if str(shot_data.get("shot_id", "")) == matching_shot_id:
            annotation = shot_data.get("annotation")
            return annotation if isinstance(annotation, dict) else {}

    raise PaletteAnalysisError(
        "No semantic image categories available for palette extraction. "
        f"No annotation found for thumbnail shot {matching_shot_id}."
    )


def _palette_diagnostic_dir(project_path: str) -> Path:
    return Path(project_path) / "outputs" / "test" / "palette"


def _write_palette_diagnostic_failure(
    project_path: str,
    media_id: str,
    filename: str,
    media_type: str,
    error: Exception,
    *,
    artifact_stem: str | None = None,
    extra: dict | None = None,
) -> Path:
    """Write a failure record and remove stale images for this media item."""
    output_dir = _palette_diagnostic_dir(project_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = artifact_stem or media_id
    for suffix in (
        "_source.png",
        "_palette.png",
        "_semantic.png",
        "_seeds.png",
        "_seeds_assignment.png",
        "_clusters.png",
    ):
        artifact = output_dir / f"{stem}{suffix}"
        if artifact.exists():
            artifact.unlink()
    json_path = output_dir / f"{stem}.json"
    document = {
        "status": "failed",
        "media_id": media_id,
        "filename": filename,
        "media_type": media_type,
        "error": str(error),
    }
    if extra:
        document.update(extra)
    json_path.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )
    return json_path


def _write_palette_diagnostic_success(
    project_path: str,
    media_id: str,
    filename: str,
    media_type: str,
    foreground: dict,
    background: dict,
    diagnostics: dict,
    diagnostic_capture: dict,
    *,
    artifact_stem: str | None = None,
    semantic_target: str | None = None,
    concepts_requested: list[str] | None = None,
    include_source: bool = True,
) -> list[Path]:
    """Render diagnostic artifacts from one completed palette extraction."""
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise ImportError(
            "Pillow is required for palette diagnostic visualization"
        ) from exc

    semantic_mask = np.asarray(
        diagnostic_capture.get("semantic_foreground_mask"), dtype=bool
    )
    if semantic_mask.shape != (_FIG_RESIZE, _FIG_RESIZE):
        raise PaletteAnalysisError(
            "Palette diagnostic semantic mask is unavailable or malformed"
        )

    captured_source = diagnostic_capture.get("source_image")
    if not isinstance(captured_source, Image.Image):
        raise PaletteAnalysisError(
            "Palette diagnostic pre-crop source image is unavailable"
        )
    source_image = captured_source.copy()
    width, height = source_image.size
    source_diagnostics = dict(diagnostics.get("source_image", {}))
    if (
        source_diagnostics.get("width") != width
        or source_diagnostics.get("height") != height
    ):
        raise PaletteAnalysisError(
            "Palette diagnostic source dimensions do not match extraction diagnostics"
        )

    try:
        foreground_rgb = tuple(int(value) for value in foreground["rgb"])
        background_rgb = tuple(int(value) for value in background["rgb"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PaletteAnalysisError(
            f"Palette diagnostic colors are malformed: {exc}"
        ) from exc
    if len(foreground_rgb) != 3 or len(background_rgb) != 3:
        raise PaletteAnalysisError("Palette diagnostic colors must be RGB triplets")

    output_dir = _palette_diagnostic_dir(project_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = artifact_stem or media_id
    source_output = output_dir / f"{stem}_source.png"
    palette_output = output_dir / f"{stem}_palette.png"
    semantic_output = output_dir / f"{stem}_semantic.png"
    seeds_output = output_dir / f"{stem}_seeds.png"
    assignment_output = output_dir / f"{stem}_seeds_assignment.png"
    clusters_output = output_dir / f"{stem}_clusters.png"
    json_output = output_dir / f"{stem}.json"

    outputs = []
    if include_source:
        source_image.save(source_output, format="PNG")
        outputs.append(source_output)

    palette_image = Image.new("RGB", (width, height), background_rgb)
    radius = max(1, int(round(min(width, height) * 0.15)))
    center_x, center_y = width // 2, height // 2
    ImageDraw.Draw(palette_image).ellipse(
        (
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
        ),
        fill=foreground_rgb,
    )
    palette_image.save(palette_output, format="PNG")

    bbox = source_diagnostics.get("content_bbox", {})
    try:
        left = int(bbox["left"])
        top = int(bbox["top"])
        right = int(bbox["right"])
        bottom = int(bbox["bottom"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PaletteAnalysisError(
            f"Palette diagnostic content bbox is malformed: {exc}"
        ) from exc
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise PaletteAnalysisError("Palette diagnostic content bbox is out of bounds")

    mask_image = Image.fromarray(
        semantic_mask.astype(np.uint8) * 255, mode="L"
    ).resize((right - left, bottom - top), Image.Resampling.NEAREST)
    full_mask = Image.new("L", (width, height), 0)
    full_mask.paste(mask_image, (left, top))
    overlay_color = Image.new("RGB", (width, height), (255, 48, 48))
    tinted = Image.blend(source_image, overlay_color, 0.45)
    semantic_image = Image.composite(tinted, source_image, full_mask)
    semantic_image.save(semantic_output, format="PNG")

    labels = np.asarray(diagnostic_capture.get("seeds_labels"))
    if labels.shape != (_FIG_RESIZE, _FIG_RESIZE):
        raise PaletteAnalysisError(
            "Palette diagnostic SEEDS labels are unavailable or malformed"
        )
    regions = diagnostic_capture.get("seeds_regions")
    if not isinstance(regions, list) or not regions:
        raise PaletteAnalysisError(
            "Palette diagnostic SEEDS assignments are unavailable"
        )
    region_by_label = {int(item["label"]): item for item in regions}
    label_ids = {int(value) for value in np.unique(labels)}
    if set(region_by_label) != label_ids:
        raise PaletteAnalysisError(
            "Palette diagnostic SEEDS assignments do not match labels"
        )

    boundaries = np.zeros(labels.shape, dtype=bool)
    horizontal = labels[:, 1:] != labels[:, :-1]
    vertical = labels[1:, :] != labels[:-1, :]
    boundaries[:, 1:] |= horizontal
    boundaries[:, :-1] |= horizontal
    boundaries[1:, :] |= vertical
    boundaries[:-1, :] |= vertical

    def _working_image_at_source(array: np.ndarray, mode: str) -> Image.Image:
        active = Image.fromarray(array, mode=mode).resize(
            (right - left, bottom - top), Image.Resampling.NEAREST
        )
        full = Image.new(mode, (width, height), 0)
        full.paste(active, (left, top))
        return full

    full_boundaries = _working_image_at_source(
        boundaries.astype(np.uint8) * 255, "L"
    )
    boundary_color = Image.new("RGB", (width, height), (0, 255, 225))
    seeds_image = Image.composite(boundary_color, source_image, full_boundaries)
    seeds_image.save(seeds_output, format="PNG")

    assignment_colors = {
        "foreground": np.asarray((235, 70, 70), dtype=np.uint8),
        "background": np.asarray((55, 115, 235), dtype=np.uint8),
        "boundary": np.asarray((255, 170, 35), dtype=np.uint8),
        "tie": np.asarray((255, 230, 55), dtype=np.uint8),
    }
    assignment_pixels = np.zeros((*labels.shape, 3), dtype=np.uint8)
    for label, region in region_by_label.items():
        category = region["assignment"]
        if region.get("boundary_crossing"):
            category = "tie" if region.get("tie") else "boundary"
        assignment_pixels[labels == label] = assignment_colors[category]
    full_assignment = _working_image_at_source(assignment_pixels, "RGB")
    active_mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(active_mask).rectangle(
        (left, top, right - 1, bottom - 1), fill=255
    )
    assignment_tint = Image.blend(source_image, full_assignment, 0.6)
    assignment_image = Image.composite(
        assignment_tint, source_image, active_mask
    )
    assignment_image = Image.composite(
        Image.new("RGB", (width, height), (20, 20, 20)),
        assignment_image,
        full_boundaries,
    )
    assignment_image.save(assignment_output, format="PNG")

    ward = diagnostic_capture.get("ward", {})
    diagnostic_cluster_colors = (
        (230, 60, 60), (40, 150, 240), (60, 190, 100), (245, 190, 45),
        (190, 80, 220), (40, 200, 200), (245, 120, 45), (120, 100, 240),
        (150, 205, 45), (245, 95, 165), (25, 135, 120), (190, 145, 35),
        (90, 180, 235), (220, 90, 110), (105, 190, 175), (170, 105, 210),
    )
    cluster_pixels = np.zeros((*labels.shape, 3), dtype=np.uint8)
    color_index = 0
    clustered_label_ids = set()
    for region_name in ("foreground", "background"):
        region_ward = ward.get(region_name, {})
        superpixel_labels = region_ward.get("superpixel_labels", [])
        cluster_labels = region_ward.get("cluster_labels", [])
        if len(superpixel_labels) != len(cluster_labels):
            raise PaletteAnalysisError(
                f"Palette diagnostic {region_name} Ward labels are malformed"
            )
        cluster_colors = {}
        for cluster_id in sorted(set(int(value) for value in cluster_labels)):
            cluster_colors[cluster_id] = diagnostic_cluster_colors[
                color_index % len(diagnostic_cluster_colors)
            ]
            color_index += 1
        for label, cluster_id in zip(superpixel_labels, cluster_labels):
            label = int(label)
            clustered_label_ids.add(label)
            cluster_pixels[labels == label] = cluster_colors[int(cluster_id)]
    if clustered_label_ids != label_ids:
        raise PaletteAnalysisError(
            "Palette diagnostic Ward memberships do not cover SEEDS labels"
        )
    full_clusters = _working_image_at_source(cluster_pixels, "RGB")
    cluster_tint = Image.blend(source_image, full_clusters, 0.68)
    clusters_image = Image.composite(cluster_tint, source_image, active_mask)
    clusters_image = Image.composite(
        Image.new("RGB", (width, height), (250, 250, 250)),
        clusters_image,
        full_boundaries,
    )
    clusters_image.save(clusters_output, format="PNG")

    semantic_keys = {
        "foreground_mode",
        "semantic_categories_used",
        "semantic_concepts_used",
        "semantic_mask_count",
        "semantic_foreground_coverage",
        "semantic_category_pixel_counts",
    }
    remaining_diagnostics = {
        key: value
        for key, value in diagnostics.items()
        if key != "source_image" and key not in semantic_keys
    }
    source_document = {
        **source_diagnostics,
        "stage": "original_before_letterbox_crop",
    }
    foreground_ward = ward.get("foreground", {})
    background_ward = ward.get("background", {})
    ward_candidate_colors = {
        "foreground": [
            list(item["rgb"])
            for item in foreground_ward.get("ranked_candidates", [])
        ],
        "background": [
            list(item["rgb"])
            for item in background_ward.get("ranked_candidates", [])
        ],
    }
    seeds_summary = {
        "count": len(region_by_label),
        "foreground_count": sum(
            item["assignment"] == "foreground" for item in regions
        ),
        "background_count": sum(
            item["assignment"] == "background" for item in regions
        ),
        "boundary_crossing_count": sum(
            bool(item.get("boundary_crossing")) for item in regions
        ),
        "tie_count": sum(bool(item.get("tie")) for item in regions),
    }
    document = {
        "status": "ok",
        "media_id": media_id,
        "filename": filename,
        "media_type": media_type,
        "source_image": source_document,
        "semantic_selection": {
            "foreground_mode": diagnostics.get("foreground_mode"),
            "categories_used": diagnostics.get("semantic_categories_used", []),
            "concepts_used": diagnostics.get("semantic_concepts_used", []),
            "mask_count": diagnostics.get("semantic_mask_count", 0),
            "foreground_coverage": diagnostics.get(
                "semantic_foreground_coverage", 0.0
            ),
            "category_pixel_counts": diagnostics.get(
                "semantic_category_pixel_counts", {}
            ),
        },
        "foreground": foreground,
        "background": background,
        "pipeline": {
            "seeds": seeds_summary,
            "ward_candidate_colors": ward_candidate_colors,
            "ward": {
                "foreground_clusters": foreground_ward.get("clusters", []),
                "background_clusters": background_ward.get("clusters", []),
            },
        },
        "diagnostics": remaining_diagnostics,
    }
    if semantic_target is not None:
        document.update({
            "semantic_target": semantic_target,
            "concepts_requested": list(concepts_requested or []),
            "number_of_masks": diagnostics.get("semantic_mask_count", 0),
            "semantic_coverage": diagnostics.get(
                "semantic_foreground_coverage", 0.0
            ),
            "seeds_count": seeds_summary["count"],
            "foreground_superpixel_count": seeds_summary["foreground_count"],
            "background_superpixel_count": seeds_summary["background_count"],
            "boundary_crossing_superpixel_count": seeds_summary[
                "boundary_crossing_count"
            ],
            "ward_candidate_colors": ward_candidate_colors,
            "final_foreground_rgb": list(foreground_rgb),
            "final_background_rgb": list(background_rgb),
        })
    json_output.write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )
    outputs.extend([
        semantic_output,
        seeds_output,
        assignment_output,
        clusters_output,
        palette_output,
        json_output,
    ])
    return outputs


def create_thumbnail_palette_semantic_diagnostic(
    project_path: str,
    filename: str,
    media_type: str,
    *,
    semantic_target: str,
    concepts: list[str],
    metadata: dict | None = None,
    segmenter=None,
    verbose: bool = False,
) -> dict:
    """Run one isolated semantic target through the production palette path."""
    requested = [
        str(concept).strip()
        for concept in concepts
        if str(concept).strip()
    ]
    if not requested:
        raise ValueError("Semantic target diagnostics require at least one concept")
    if metadata is None:
        entries = get_metadata(project_path, media_type=media_type)
        metadata = next(
            (entry for entry in entries if entry.get("filename") == filename),
            {"filename": filename},
        )
    media_id = str(
        metadata.get("media_id") or compute_media_id(metadata, media_type)
    )
    safe_target = "".join(
        character if character.isalnum() or character in "_-" else "_"
        for character in semantic_target.strip().lower()
    ).strip("_")
    if not safe_target:
        raise ValueError("Semantic target diagnostic name is empty")
    artifact_stem = f"{media_id}_{safe_target}"
    target_failure_fields = {
        "semantic_target": semantic_target,
        "concepts_requested": requested,
        "number_of_masks": None,
        "semantic_coverage": None,
        "seeds_count": None,
        "foreground_superpixel_count": None,
        "background_superpixel_count": None,
        "boundary_crossing_superpixel_count": None,
        "ward_candidate_colors": {"foreground": [], "background": []},
        "final_foreground_rgb": None,
        "final_background_rgb": None,
    }
    source_path = resolve_thumbnail_path(project_path, media_type, filename)
    if source_path is None:
        error = FileNotFoundError(
            f"Metadata thumbnail not found for '{filename}' ({media_type})"
        )
        _write_palette_diagnostic_failure(
            project_path,
            media_id,
            filename,
            media_type,
            error,
            artifact_stem=artifact_stem,
            extra=target_failure_fields,
        )
        raise error

    annotation = {
        "humans": requested,
        "animals": [],
        "objects": [],
        "setting": "",
    }
    diagnostic_capture: dict = {}
    try:
        foreground, background, diagnostics = _extract_fg_bg_full(
            source_path,
            annotation=annotation,
            segmenter=segmenter or _load_palette_segmenter(project_path, verbose),
            diagnostic_capture=diagnostic_capture,
        )
        files = _write_palette_diagnostic_success(
            project_path,
            media_id,
            filename,
            media_type,
            foreground,
            background,
            diagnostics,
            diagnostic_capture,
            artifact_stem=artifact_stem,
            semantic_target=semantic_target,
            concepts_requested=requested,
            include_source=False,
        )
    except Exception as exc:
        failure_fields = dict(target_failure_fields)
        if "no usable masks" in str(exc):
            failure_fields["number_of_masks"] = 0
            failure_fields["semantic_coverage"] = 0.0
        _write_palette_diagnostic_failure(
            project_path,
            media_id,
            filename,
            media_type,
            exc,
            artifact_stem=artifact_stem,
            extra=failure_fields,
        )
        raise
    return {
        "media_id": media_id,
        "semantic_target": semantic_target,
        "concepts_requested": requested,
        "foreground": foreground,
        "background": background,
        "diagnostics": diagnostics,
        "diagnostic_files": [str(path) for path in files],
    }


def create_thumbnail_palette(
    project_path: str,
    filename: str,
    media_type: str = "movie",
    *,
    metadata: dict | None = None,
    force: bool = False,
    verbose: bool = False,
    visualize: bool = False,
) -> dict:
    """Extract a thumbnail palette and optionally write diagnostic artifacts."""
    try:
        return _create_thumbnail_palette(
            project_path,
            filename,
            media_type,
            metadata=metadata,
            force=force,
            verbose=verbose,
            visualize=visualize,
        )
    except Exception as exc:
        if visualize:
            failure_metadata = metadata
            if failure_metadata is None:
                try:
                    entries = get_metadata(project_path, media_type=media_type)
                    failure_metadata = next(
                        (
                            entry for entry in entries
                            if entry.get("filename") == filename
                        ),
                        None,
                    )
                except Exception:
                    failure_metadata = None
            failure_metadata = failure_metadata or {"filename": filename}
            try:
                media_id = str(
                    failure_metadata.get("media_id")
                    or compute_media_id(failure_metadata, media_type)
                )
            except Exception:
                media_id = Path(filename).stem
            _write_palette_diagnostic_failure(
                project_path, media_id, filename, media_type, exc
            )
        raise


def _create_thumbnail_palette(
    project_path: str,
    filename: str,
    media_type: str = "movie",
    *,
    metadata: dict | None = None,
    force: bool = False,
    verbose: bool = False,
    visualize: bool = False,
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
    annotation = _resolve_thumbnail_annotation(
        project_path, filename, media_type, media_id, metadata
    )
    semantic_input = _semantic_input_fingerprint(annotation)
    cache_path = get_thumbnail_palette_path(project_path, media_id, media_type)
    existing = load_thumbnail_palette(project_path, media_id, media_type)
    if existing is not None and not force:
        if not _semantic_cache_is_current(existing):
            raise FileExistsError(
                f"Legacy thumbnail palette cache exists: {cache_path}\n"
                "  Pass --force to replace it with semantic analysis."
            )
        if _thumbnail_cache_is_current(existing, source_image, semantic_input):
            if verbose:
                note = (
                    "; diagnostics require --force"
                    if visualize else ""
                )
                print(
                    f"  skip  {filename}: thumbnail palette already cached{note}"
                )
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

    diagnostic_capture = {} if visualize else None
    extraction_kwargs = {
        "annotation": annotation,
        "segmenter": _load_palette_segmenter(project_path, verbose),
    }
    if diagnostic_capture is not None:
        extraction_kwargs["diagnostic_capture"] = diagnostic_capture
    foreground, background, diagnostics = _extract_fg_bg_full(
        source_path, **extraction_kwargs
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
        "schema_version": PALETTE_SCHEMA_VERSION,
        "analysis_version": PALETTE_ANALYSIS_VERSION,
        "media": identity,
        "source": "thumbnail",
        "source_image": source_image,
        "semantic_input": semantic_input,
        "method": "semantic-figure",
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
    diagnostic_files = []
    if diagnostic_capture is not None:
        diagnostic_files = _write_palette_diagnostic_success(
            project_path,
            media_id,
            filename,
            media_type,
            foreground,
            background,
            diagnostics,
            diagnostic_capture,
        )
        if verbose:
            print(
                f"  → diagnostics  "
                f"{_palette_diagnostic_dir(project_path).relative_to(project_path)}"
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
        "diagnostic_files": [str(path) for path in diagnostic_files],
    }


def create_thumbnail_palettes_for_all(
    project_path: str,
    media_type: str = "movie",
    *,
    force: bool = False,
    verbose: bool = False,
    visualize: bool = False,
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
                visualize=visualize,
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
            png_path,
            annotation=shot_data.get("annotation"),
            segmenter=segmenter,
        )
    except Exception as exc:
        return {**base, "status": "error", "reason": str(exc)}

    result: dict = {
        **base,
        "foreground": foreground,
        "background": background,
        "method": "semantic-figure",
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
        existing = load_palette(project_path, filename, media_type)
        if not _semantic_cache_is_current(existing):
            raise FileExistsError(
                f"Legacy palette cache exists: {cache_path}\n"
                "  Pass --force to replace it with semantic analysis."
            )
        # Check whether either canonical input is newer than the palette cache.
        from data.annotate import get_annotation_json_path

        shotlist_path = get_shotlist_path(project_path, filename, media_type)
        annotation_path = get_annotation_json_path(
            project_path, filename, media_type
        )
        stale = any(
            source_path.exists()
            and source_path.stat().st_mtime_ns > cache_path.stat().st_mtime_ns
            for source_path in (shotlist_path, annotation_path)
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
            print(
                f"  stale {filename}: source data is newer than palette cache "
                "— regenerating"
            )

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
        "schema_version": PALETTE_SCHEMA_VERSION,
        "analysis_version": PALETTE_ANALYSIS_VERSION,
        "movie": movie_block,
        "source": "best_frame",
        "method": "semantic-figure",
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
