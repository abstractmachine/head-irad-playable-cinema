"""Page-aware preprocessing for adaptive engraving generation.

Prepares a placed silhouette for a future engraving generator by:

1. Computing a target canvas size from the object's on-page footprint
   (physical page size in PDF points × target DPI, not the on-screen pixels).
2. Resizing the source PNG (RGBA, preserving alpha) to that canvas.
3. Applying flip and rotation transforms.
4. Writing the result to a deterministic per-engraving cache path.
5. Returning the output path and a JSON sidecar with all metadata.

This module produces NO stylistic engraving effects.  The output is the
clean, page-scaled source that a future generator will consume.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREPROCESS_VERSION = "v1"

# Reference DPI used to derive pixel dimensions from physical page size.
# 150 DPI gives ~1240 px for an A4-width page — a comfortable generator input.
_PAGE_DPI: int = 150

# Canvas size guard-rails (pixels, per dimension).
_MIN_DIM: int  = 128
_MAX_DIM: int  = 4096


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def build_cache_key(
    source_png: str | Path,
    page_idx: int,
    x: float,
    y: float,
    width: float,
    height: float,
    rotation: float,
    flip_h: bool,
    flip_v: bool,
    version: str = PREPROCESS_VERSION,
) -> str:
    """Return a short deterministic hex key for the given geometry fingerprint."""
    payload = json.dumps(
        {
            "source": str(source_png),
            "page":   page_idx,
            "x":      round(x,        6),
            "y":      round(y,        6),
            "w":      round(width,    6),
            "h":      round(height,   6),
            "rot":    round(rotation, 3),
            "flip_h": bool(flip_h),
            "flip_v": bool(flip_v),
            "version": version,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Target-size computation
# ---------------------------------------------------------------------------

def compute_target_size(
    width_frac: float,
    height_frac: float,
    page_pt_w: float,
    page_pt_h: float,
    dpi: int = _PAGE_DPI,
    min_dim: int = _MIN_DIM,
    max_dim: int = _MAX_DIM,
) -> tuple[int, int]:
    """Return (target_w, target_h) in pixels for a placed silhouette.

    Parameters
    ----------
    width_frac, height_frac
        Normalised fractions [0, 1] of the rendered page dimensions.
    page_pt_w, page_pt_h
        PDF page dimensions in points (1 pt = 1/72 inch).
    dpi
        Reference DPI used to convert physical inches → pixels.
    min_dim, max_dim
        Clamp range applied to each pixel dimension independently.
    """
    # Physical footprint in inches
    w_in = width_frac  * page_pt_w / 72.0
    h_in = height_frac * page_pt_h / 72.0

    tw = int(round(w_in * dpi))
    th = int(round(h_in * dpi))

    tw = max(min_dim, min(max_dim, tw))
    th = max(min_dim, min(max_dim, th))
    return tw, th


# ---------------------------------------------------------------------------
# Core preprocessing
# ---------------------------------------------------------------------------

def preprocess_engraving_source(
    *,
    source_png: str | Path,
    engraving_id: str,
    parent_layer_id: str,
    source_silhouette_id: str,
    page_idx: int,
    x: float,
    y: float,
    width: float,
    height: float,
    rotation: float,
    flip_h: bool,
    flip_v: bool,
    page_pt_w: float,
    page_pt_h: float,
    cache_dir: Path,
    dpi: int = _PAGE_DPI,
) -> dict:
    """Preprocess a placed silhouette and write the result to *cache_dir*.

    Parameters
    ----------
    source_png
        Absolute path to the source silhouette PNG (RGBA or RGB).
    engraving_id
        Unique id of the engraving layer (used to name the output files).
    parent_layer_id
        Layer id of the parent silhouette placement.
    source_silhouette_id
        Relative path / identifier stored in the parent layer's ``source`` field.
    page_idx
        0-based page index in the PDF.
    x, y
        Normalised centre coordinates [0, 1] of the placed silhouette on the page.
    width, height
        Normalised fractions [0, 1] of the page dimensions.
    rotation
        Clockwise rotation in degrees (matching Qt's coordinate system).
    flip_h, flip_v
        Horizontal and vertical flip flags.
    page_pt_w, page_pt_h
        PDF page dimensions in points (from ``page.rect.width / .height``).
    cache_dir
        Directory where the PNG and sidecar JSON will be written.
    dpi
        Reference DPI for target-size computation (default 150).

    Returns
    -------
    dict with keys:
        preprocessing_path : str   — absolute path of the output PNG
        preprocessing_size : list  — [width_px, height_px]
        cache_key          : str   — hex fingerprint of the geometry
        version            : str   — preprocessing version tag
    """
    from PIL import Image

    source_png = Path(source_png)
    cache_dir  = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_key = build_cache_key(
        source_png, page_idx, x, y, width, height, rotation, flip_h, flip_v,
    )

    out_png  = cache_dir / f"{engraving_id}_preprocess_{PREPROCESS_VERSION}.png"
    out_json = cache_dir / f"{engraving_id}_preprocess_{PREPROCESS_VERSION}.json"

    # ------------------------------------------------------------------
    # Cache hit — reuse if the geometry fingerprint still matches
    # ------------------------------------------------------------------
    if out_json.exists() and out_png.exists():
        try:
            saved = json.loads(out_json.read_text(encoding="utf-8"))
            if saved.get("cache_key") == cache_key:
                return {
                    "preprocessing_path": str(out_png),
                    "preprocessing_size": saved["preprocessing_size"],
                    "cache_key":          cache_key,
                    "version":            PREPROCESS_VERSION,
                }
        except Exception:
            pass  # damaged sidecar — regenerate

    # ------------------------------------------------------------------
    # Compute target canvas
    # ------------------------------------------------------------------
    tw, th = compute_target_size(width, height, page_pt_w, page_pt_h, dpi=dpi)

    # ------------------------------------------------------------------
    # Load source image
    # ------------------------------------------------------------------
    try:
        img = Image.open(source_png).convert("RGBA")
    except Exception as exc:
        raise RuntimeError(
            f"engraving_preprocess: cannot open source PNG {source_png!r}: {exc}"
        ) from exc

    # ------------------------------------------------------------------
    # Resize to target canvas (LANCZOS for quality)
    # ------------------------------------------------------------------
    img = img.resize((tw, th), Image.LANCZOS)

    # ------------------------------------------------------------------
    # Apply flip transforms
    # ------------------------------------------------------------------
    if flip_h:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if flip_v:
        img = img.transpose(Image.FLIP_TOP_BOTTOM)

    # ------------------------------------------------------------------
    # Apply rotation
    # Qt uses clockwise rotation with y-axis pointing down.
    # PIL.Image.rotate uses counter-clockwise, so we negate.
    # expand=False keeps the canvas at (tw, th) — consistent with the
    # on-page footprint; pixels outside the bounding box are transparent.
    # ------------------------------------------------------------------
    if rotation:
        img = img.rotate(
            -rotation,
            resample=Image.BICUBIC,
            expand=False,
            fillcolor=(0, 0, 0, 0),
        )

    # ------------------------------------------------------------------
    # Write output PNG
    # ------------------------------------------------------------------
    img.save(str(out_png), format="PNG", optimize=False)

    # ------------------------------------------------------------------
    # Write sidecar JSON
    # ------------------------------------------------------------------
    sidecar = {
        "version":             PREPROCESS_VERSION,
        "cache_key":           cache_key,
        "engraving_id":        engraving_id,
        "parent_layer_id":     parent_layer_id,
        "source_silhouette_id": source_silhouette_id,
        "source_png":          str(source_png),
        "page_idx":            page_idx,
        "x":                   x,
        "y":                   y,
        "width":               width,
        "height":              height,
        "rotation":            rotation,
        "flip_h":              flip_h,
        "flip_v":              flip_v,
        "page_pt_w":           page_pt_w,
        "page_pt_h":           page_pt_h,
        "dpi":                 dpi,
        "preprocessing_size":  [tw, th],
        "preprocessing_path":  str(out_png),
    }
    out_json.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "preprocessing_path": str(out_png),
        "preprocessing_size": [tw, th],
        "cache_key":          cache_key,
        "version":            PREPROCESS_VERSION,
    }
