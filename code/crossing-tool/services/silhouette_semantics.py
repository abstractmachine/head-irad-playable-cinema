"""Silhouette semantic analysis — geometry, CLIP classification, and engraving score.

This module enriches existing silhouette catalog JSON metadata with semantic
fields that allow retrieval by illustration usefulness rather than pixel area.

It is designed to be called *after* silhouettes have already been extracted.
It never regenerates PNGs, never reruns SAM, and never reads the original
video.  All inputs are the transparent PNG and the existing JSON metadata.

Pipeline
--------
1. Deterministic geometry — derived from the PNG alpha channel and the
   existing ``bbox`` / ``frame_size`` metadata.  No AI required.
2. CLIP semantic classification — the PNG is composited onto a neutral grey
   background and scored against structured prompt texts loaded from the
   project's prompt file (``prompts/silhouettes/*.txt``, latest wins).
3. Composite ``engraving_score`` — weighted geometric mean of the four
   semantic dimension scores with a hard cap for frame-touching objects.

Public API
----------
``analyze_silhouette_semantics(png_path, meta, ...)``
    Analyse one silhouette.  Returns a flat dict of new semantic fields.

``enrich_catalog(project_path, ...)``
    Batch-enrich the entire catalog (or a filtered subset).  Calls
    ``analyze_silhouette_semantics`` for each entry and writes results back
    into the JSON in place.  Skips entries that already carry
    ``semantic_version``.  Supports ``--rebuild`` to force re-enrichment.

Schema produced
---------------
{
    "semantic_version":   "1",

    # Deterministic geometry
    "touches_frame":      false,
    "edge_touch":         ["top"],
    "center_offset_norm": [0.12, -0.05],
    "fill_ratio":         0.87,
    "fragment_count":     1,
    "bbox_aspect_ratio":  1.42,
    "edge_score":         1.0,

    # Semantic labels (CLIP zero-shot)
    "viewpoint":          "side",
    "completeness":       "full",
    "occlusion":          "none",
    "isolation":          "isolated",

    # Numeric scores derived from labels
    "completeness_score":   1.0,
    "occlusion_score":      1.0,
    "viewpoint_score_sem":  1.0,
    "isolation_score":      1.0,

    # Composite
    "engraving_score":      0.88,
    "semantic_confidence":  0.91,

    "semantics_generated": "2026-07-04T..."
}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Version tag written into every enriched JSON
# ---------------------------------------------------------------------------

SEMANTIC_VERSION = "1"

# ---------------------------------------------------------------------------
# Label → numeric score tables
# ---------------------------------------------------------------------------

_COMPLETENESS_SCORES: Dict[str, float] = {
    "full":    1.00,
    "partial": 0.45,
    "cropped": 0.10,
}

_OCCLUSION_SCORES: Dict[str, float] = {
    "none":  1.00,
    "mild":  0.65,
    "heavy": 0.20,
}

_VIEWPOINT_SCORES: Dict[str, float] = {
    "side":          1.00,
    "three_quarter": 0.85,
    "front":         0.75,
    "rear":          0.70,
    "overhead":      0.50,
    "unknown":       0.40,
}

_ISOLATION_SCORES: Dict[str, float] = {
    "isolated":    1.00,
    "overlapping": 0.60,
}

# Edge-score constants
_EDGE_SCORE_NO_TOUCH   = 1.00
_EDGE_SCORE_ONE_EDGE   = 0.50
_EDGE_SCORE_MULTI_EDGE = 0.20

# Hard cap applied when touches_frame is True
_ENGRAVING_SCORE_FRAME_CAP = 0.30

# Engraving score exponents (weighted geometric mean; must sum to 1.0)
_W_COMPLETENESS = 0.30
_W_OCCLUSION    = 0.20
_W_EDGE         = 0.25
_W_ISOLATION    = 0.15
_W_VIEWPOINT    = 0.10

# Border check threshold (pixels from edge)
_BORDER_CHECK_PX = 3


# ---------------------------------------------------------------------------
# 1. Deterministic geometry
# ---------------------------------------------------------------------------

def _compute_geometry(
    png_path: Path,
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    """Return deterministic geometry fields from the PNG and existing metadata.

    All computation is pure NumPy / PIL / cv2 — no AI required.

    Parameters
    ----------
    png_path:  Path to the transparent RGBA PNG.
    meta:      Existing JSON metadata dict (for ``bbox`` and ``frame_size``).

    Returns
    -------
    Flat dict of geometry fields (see module docstring).
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow and numpy are required for silhouette geometry analysis.\n"
            "Install with: pip install Pillow numpy"
        ) from exc

    # -- Load alpha mask from PNG ------------------------------------------
    img = Image.open(str(png_path)).convert("RGBA")
    arr = np.array(img, dtype=np.uint8)
    mask = arr[:, :, 3] > 127          # boolean alpha mask (H, W)
    mask_h, mask_w = mask.shape

    frame_size = meta.get("frame_size") or [0, 0]
    frame_w = int(frame_size[0]) if len(frame_size) >= 2 else mask_w
    frame_h = int(frame_size[1]) if len(frame_size) >= 2 else mask_h

    bbox = meta.get("bbox") or []
    if len(bbox) >= 4:
        bx, by, bw, bh = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
    else:
        bx, by, bw, bh = 0, 0, mask_w, mask_h

    # -- Edge touch -----------------------------------------------------------
    # Reconstruct the full-frame mask from the cropped PNG alpha and bbox.
    # The PNG is cropped to the bbox (with pad_px), so the mask in the PNG
    # coordinate space corresponds to rows [by..by+mask_h] and
    # cols [bx..bx+mask_w] in frame space.  We check whether any mask pixel
    # lands within _BORDER_CHECK_PX of the full-frame border.

    edges_touched: List[str] = []
    if frame_w > 0 and frame_h > 0:
        border = _BORDER_CHECK_PX
        # Top: bbox top-edge (by) is within border pixels of frame top (0)
        if by <= border and mask[:border - by, :].any() if by < border else False:
            edges_touched.append("top")
        elif by <= border:
            # bbox top is close to frame top; check whether any mask pixel
            # is in the rows that correspond to frame rows 0..border-1
            rows_in_frame_top = border - by
            if rows_in_frame_top > 0 and mask[:rows_in_frame_top, :].any():
                edges_touched.append("top")

        # Bottom
        frame_bottom_in_mask = (frame_h - border) - by
        if 0 <= frame_bottom_in_mask < mask_h and mask[frame_bottom_in_mask:, :].any():
            edges_touched.append("bottom")
        elif by + mask_h >= frame_h - border:
            edges_touched.append("bottom")

        # Left
        if bx <= border and mask[:, :border - bx].any() if bx < border else False:
            edges_touched.append("left")
        elif bx <= border:
            cols_in_frame_left = border - bx
            if cols_in_frame_left > 0 and mask[:, :cols_in_frame_left].any():
                edges_touched.append("left")

        # Right
        frame_right_in_mask = (frame_w - border) - bx
        if 0 <= frame_right_in_mask < mask_w and mask[:, frame_right_in_mask:].any():
            edges_touched.append("right")
        elif bx + mask_w >= frame_w - border:
            edges_touched.append("right")

    # Simpler bbox-level edge check as a backstop (same logic as scoring.py)
    bbox_touches: List[str] = []
    if frame_w > 0 and frame_h > 0:
        b = _BORDER_CHECK_PX
        if bx <= b:
            bbox_touches.append("left")
        if by <= b:
            bbox_touches.append("top")
        if bx + bw >= frame_w - b:
            bbox_touches.append("right")
        if by + bh >= frame_h - b:
            bbox_touches.append("bottom")

    # Union of pixel-level and bbox-level checks (bbox is conservative fallback)
    all_touched = sorted(set(edges_touched) | set(bbox_touches))
    touches_frame = len(all_touched) > 0

    n_touched = len(all_touched)
    if n_touched == 0:
        edge_score = _EDGE_SCORE_NO_TOUCH
    elif n_touched == 1:
        edge_score = _EDGE_SCORE_ONE_EDGE
    else:
        edge_score = _EDGE_SCORE_MULTI_EDGE

    # -- Center offset (normalized) -------------------------------------------
    # Centroid of mask pixels in frame coordinates.
    rows_idx, cols_idx = np.where(mask)
    if len(rows_idx) > 0:
        centroid_row = float(rows_idx.mean()) + by   # frame y
        centroid_col = float(cols_idx.mean()) + bx   # frame x
    else:
        centroid_row = by + bh / 2.0
        centroid_col = bx + bw / 2.0

    cx_norm = ((centroid_col - frame_w / 2.0) / (frame_w / 2.0)) if frame_w > 0 else 0.0
    cy_norm = ((centroid_row - frame_h / 2.0) / (frame_h / 2.0)) if frame_h > 0 else 0.0
    cx_norm = max(-1.0, min(1.0, cx_norm))
    cy_norm = max(-1.0, min(1.0, cy_norm))

    # -- Fill ratio (mask / bbox) -----------------------------------------------
    mask_area = int(mask.sum())
    bbox_area = max(1, bw * bh)
    fill_ratio = min(1.0, mask_area / bbox_area)

    # -- Bounding box aspect ratio -------------------------------------------
    bbox_aspect_ratio = (bw / bh) if bh > 0 else 1.0

    # -- Fragment count (connected components) --------------------------------
    fragment_count = 1
    try:
        import cv2
        contours, _ = cv2.findContours(
            (mask.astype(np.uint8) * 255),
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        fragment_count = max(1, len(contours))
    except Exception:
        pass

    return {
        "touches_frame":      touches_frame,
        "edge_touch":         all_touched,
        "center_offset_norm": [round(cx_norm, 4), round(cy_norm, 4)],
        "fill_ratio":         round(fill_ratio, 6),
        "fragment_count":     fragment_count,
        "bbox_aspect_ratio":  round(bbox_aspect_ratio, 4),
        "edge_score":         round(edge_score, 4),
    }


# ---------------------------------------------------------------------------
# 2. CLIP semantic classification
# ---------------------------------------------------------------------------

def _composite_rgba_for_clip(png_path: Path):
    """Composite RGBA PNG onto neutral grey → PIL RGB image for CLIP scoring."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(str(png_path)).convert("RGBA")
        bg = Image.new("RGB", img.size, (128, 128, 128))
        bg.paste(img.convert("RGB"), mask=img.split()[-1])
        return bg
    except Exception:
        return None


def _clip_classify_dimension(
    composite_image,
    templates: Dict[str, str],
    label: str,
    field: str,
    clip_model,
    clip_processor,
    clip_device,
) -> Tuple[str, float, float]:
    """Score *composite_image* against each prompt in *templates* via CLIP.

    Returns
    -------
    (winning_label, winning_score, confidence)
    winning_score is the raw cosine-similarity score.
    confidence is the softmax probability of the winning label.
    Returns ("unknown", 0.0, 0.0) on any failure.
    """
    from services.silhouette_prompt import expand_prompt

    if composite_image is None or not templates:
        return "unknown", 0.0, 0.0

    try:
        import numpy as np
        from services.frame_match import clip_score_batch

        labels_ordered = list(templates.keys())
        prompt_texts   = [
            expand_prompt(templates[lbl], label=label, field=field)
            for lbl in labels_ordered
        ]

        scores = []
        for text in prompt_texts:
            s = clip_score_batch(
                [composite_image], text,
                clip_model, clip_processor, clip_device,
            )[0]
            scores.append(float(s))

        scores_arr = np.array(scores, dtype=np.float64)
        # Softmax for confidence estimation
        exp_s = np.exp(scores_arr - scores_arr.max())
        softmax = exp_s / exp_s.sum()

        best_idx = int(scores_arr.argmax())
        return (
            labels_ordered[best_idx],
            round(float(scores_arr[best_idx]), 6),
            round(float(softmax[best_idx]), 6),
        )

    except Exception:
        return "unknown", 0.0, 0.0


def _classify_semantics(
    png_path: Path,
    label: str,
    field: str,
    prompts: Dict[str, Dict[str, str]],
    clip_model,
    clip_processor,
    clip_device,
) -> Dict[str, Any]:
    """Run CLIP classification for all dimensions in *prompts*.

    Returns a flat dict with label fields and their numeric scores, plus
    ``semantic_confidence`` (mean softmax confidence across dimensions).
    """
    composite = _composite_rgba_for_clip(png_path)

    results: Dict[str, Any] = {}
    confidences: List[float] = []

    # viewpoint
    if "viewpoint" in prompts:
        lbl, _, conf = _clip_classify_dimension(
            composite, prompts["viewpoint"], label, field,
            clip_model, clip_processor, clip_device,
        )
        results["viewpoint"]           = lbl
        results["viewpoint_score_sem"] = round(_VIEWPOINT_SCORES.get(lbl, 0.40), 6)
        confidences.append(conf)
    else:
        results["viewpoint"]           = "unknown"
        results["viewpoint_score_sem"] = _VIEWPOINT_SCORES["unknown"]

    # completeness
    if "completeness" in prompts:
        lbl, _, conf = _clip_classify_dimension(
            composite, prompts["completeness"], label, field,
            clip_model, clip_processor, clip_device,
        )
        results["completeness"]       = lbl
        results["completeness_score"] = round(_COMPLETENESS_SCORES.get(lbl, 0.45), 6)
        confidences.append(conf)
    else:
        results["completeness"]       = "full"
        results["completeness_score"] = 1.0

    # occlusion
    if "occlusion" in prompts:
        lbl, _, conf = _clip_classify_dimension(
            composite, prompts["occlusion"], label, field,
            clip_model, clip_processor, clip_device,
        )
        results["occlusion"]       = lbl
        results["occlusion_score"] = round(_OCCLUSION_SCORES.get(lbl, 1.0), 6)
        confidences.append(conf)
    else:
        results["occlusion"]       = "none"
        results["occlusion_score"] = 1.0

    # isolation
    if "isolation" in prompts:
        lbl, _, conf = _clip_classify_dimension(
            composite, prompts["isolation"], label, field,
            clip_model, clip_processor, clip_device,
        )
        results["isolation"]       = lbl
        results["isolation_score"] = round(_ISOLATION_SCORES.get(lbl, 0.60), 6)
        confidences.append(conf)
    else:
        results["isolation"]       = "isolated"
        results["isolation_score"] = 1.0

    results["semantic_confidence"] = round(
        float(sum(confidences) / len(confidences)) if confidences else 0.0,
        6,
    )
    return results


# ---------------------------------------------------------------------------
# 3. Engraving score
# ---------------------------------------------------------------------------

def _compute_engraving_score(
    completeness_score: float,
    occlusion_score:    float,
    edge_score:         float,
    isolation_score:    float,
    viewpoint_score:    float,
    touches_frame:      bool,
) -> float:
    """Weighted geometric mean of semantic quality dimensions.

    Exponents:
        completeness  0.30
        edge          0.25
        occlusion     0.20
        isolation     0.15
        viewpoint     0.10

    A hard cap of 0.30 is applied when *touches_frame* is True, preventing
    frame-touching objects from reaching top retrieval positions.
    """
    def _safe(v: float) -> float:
        return max(1e-9, min(1.0, float(v)))

    score = (
        _safe(completeness_score) ** _W_COMPLETENESS
        * _safe(occlusion_score)  ** _W_OCCLUSION
        * _safe(edge_score)       ** _W_EDGE
        * _safe(isolation_score)  ** _W_ISOLATION
        * _safe(viewpoint_score)  ** _W_VIEWPOINT
    )

    score = max(0.0, min(1.0, score))
    if touches_frame:
        score = min(score, _ENGRAVING_SCORE_FRAME_CAP)

    return round(score, 6)


# ---------------------------------------------------------------------------
# Public analysis function
# ---------------------------------------------------------------------------

def analyze_silhouette_semantics(
    png_path: Path,
    meta: Dict[str, Any],
    *,
    clip_model=None,
    clip_processor=None,
    clip_device=None,
    prompts: Optional[Dict[str, Dict[str, str]]] = None,
    project_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyse one silhouette and return a dict of new semantic fields.

    This function does **not** modify any file.  The caller is responsible for
    merging the returned dict into the existing JSON and persisting it.

    Parameters
    ----------
    png_path:       Path to the transparent RGBA PNG.
    meta:           Existing JSON metadata dict.  Used for ``bbox``,
                    ``frame_size``, ``label``, and ``field``.
    clip_model:     Pre-loaded CLIP model (optional; skips classification when
                    None).
    clip_processor: Pre-loaded CLIP processor.
    clip_device:    CLIP device string (e.g. ``"cuda"``).
    prompts:        Pre-loaded prompts dict from ``load_silhouette_prompts()``.
                    If None and ``project_path`` is given, the prompt file is
                    loaded automatically.  If both are None, semantic
                    classification is skipped and geometry-only fields are
                    returned.
    project_path:   Project root path; used to load the prompt file when
                    *prompts* is not provided.

    Returns
    -------
    Flat dict of semantic fields (see module docstring for the full schema).
    Includes ``semantic_version`` and ``semantics_generated``.
    """
    # -- Resolve prompts -------------------------------------------------------
    prompt_filename: Optional[str] = None
    if prompts is None and project_path is not None:
        try:
            from services.silhouette_prompt import load_silhouette_prompts
            prompt_filename, prompts = load_silhouette_prompts(project_path)
        except Exception:
            prompts = None

    # -- Geometry --------------------------------------------------------------
    geo = _compute_geometry(png_path, meta)

    # -- Semantic classification -----------------------------------------------
    label = (meta.get("label") or "").strip() or "object"
    field = (meta.get("field") or "").strip()

    sem: Dict[str, Any]
    if clip_model is not None and clip_processor is not None and prompts:
        sem = _classify_semantics(
            png_path, label, field, prompts,
            clip_model, clip_processor, clip_device,
        )
    else:
        # Geometry-only fallback — semantic labels default to most-positive values
        sem = {
            "viewpoint":           "unknown",
            "viewpoint_score_sem": _VIEWPOINT_SCORES["unknown"],
            "completeness":        "full",
            "completeness_score":  1.0,
            "occlusion":           "none",
            "occlusion_score":     1.0,
            "isolation":           "isolated",
            "isolation_score":     1.0,
            "semantic_confidence": 0.0,
        }

    # -- Engraving score -------------------------------------------------------
    engraving_score = _compute_engraving_score(
        completeness_score = sem["completeness_score"],
        occlusion_score    = sem["occlusion_score"],
        edge_score         = geo["edge_score"],
        isolation_score    = sem["isolation_score"],
        viewpoint_score    = sem["viewpoint_score_sem"],
        touches_frame      = geo["touches_frame"],
    )

    result: Dict[str, Any] = {
        "semantic_version": SEMANTIC_VERSION,
        **geo,
        **sem,
        "engraving_score": engraving_score,
        "semantics_generated": datetime.now(timezone.utc).isoformat(),
    }
    if prompt_filename:
        result["semantics_prompt_file"] = prompt_filename

    return result


# ---------------------------------------------------------------------------
# Batch enrichment
# ---------------------------------------------------------------------------

def enrich_catalog(
    project_path: str,
    media_type: str = "movie",
    label: Optional[str] = None,
    field: Optional[str] = None,
    rebuild: bool = False,
    verbose: bool = False,
    model_name: Optional[str] = None,
) -> Dict[str, int]:
    """Enrich every catalog entry with semantic metadata.

    Loads the CLIP model and prompt file once, then iterates over all catalog
    entries, skipping those that already carry ``semantic_version`` (unless
    *rebuild* is True).  Results are written back into the JSON in place.
    The corresponding PNG is never touched.

    Returns a summary dict with keys: ``processed``, ``skipped``, ``errors``.
    """
    from services.silhouette_catalog import scan_catalog
    from services.silhouette_prompt import load_silhouette_prompts, SilhouettePromptError

    # -- Load prompts ----------------------------------------------------------
    try:
        prompt_filename, prompts = load_silhouette_prompts(project_path)
        if verbose:
            print(f"  Using prompt file: {prompt_filename}")
    except SilhouettePromptError as exc:
        print(f"✗ {exc}")
        return {"processed": 0, "skipped": 0, "errors": 1}

    # -- Load CLIP model -------------------------------------------------------
    clip_model = clip_processor = clip_device = None
    try:
        from tool import prefs as _prefs
        from services.frame_match import _load_clip_model
        model_choice = model_name or _prefs.get("model_frame_match", "clip-vit-base-patch32")
        clip_model, clip_processor, clip_device = _load_clip_model(project_path, model_choice)
        if verbose:
            print(f"  CLIP model loaded: {model_choice}")
    except Exception as exc:
        if verbose:
            print(f"  Warning: could not load CLIP model ({exc}); "
                  f"geometry-only enrichment will run.")

    # -- Scan catalog ----------------------------------------------------------
    records = scan_catalog(project_path, media_type=media_type, label=label)
    if field is not None:
        records = [r for r in records if (r.get("field") or "") == field]

    if not records:
        return {"processed": 0, "skipped": 0, "errors": 0}

    processed = skipped = errors = 0

    for rec in records:
        try:
            json_path = Path(rec.get("path"))
            if not json_path.exists():
                errors += 1
                continue

            meta = json.loads(json_path.read_text(encoding="utf-8"))

            if not rebuild and meta.get("semantic_version") is not None:
                skipped += 1
                continue

            png_name = meta.get("png", "")
            if png_name:
                png_path = json_path.parent / png_name
            else:
                png_path = json_path.with_suffix(".png")

            if not png_path.exists():
                if verbose:
                    print(f"  ✗ PNG not found: {png_path}")
                errors += 1
                continue

            semantic_fields = analyze_silhouette_semantics(
                png_path,
                meta,
                clip_model=clip_model,
                clip_processor=clip_processor,
                clip_device=clip_device,
                prompts=prompts,
                project_path=project_path,
            )
            # Always record the prompt file used
            semantic_fields["semantics_prompt_file"] = prompt_filename

            meta.update(semantic_fields)
            from data.annotate import atomic_write_text

            atomic_write_text(json_path, json.dumps(meta, indent=2, ensure_ascii=False))
            processed += 1

            if verbose:
                print(
                    f"  Enriched: {json_path.name}"
                    f"  completeness={meta.get('completeness')}"
                    f"  viewpoint={meta.get('viewpoint')}"
                    f"  engraving_score={meta.get('engraving_score'):.3f}"
                )

        except Exception as exc:
            errors += 1
            if verbose:
                print(f"  ✗ Error enriching {rec.get('path')}: {exc}")

    return {"processed": processed, "skipped": skipped, "errors": errors}
