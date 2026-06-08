"""Silhouette quality scoring and CLI integration.

This module computes a set of lightweight quality metrics for each object
in the silhouette catalog and persists numeric score fields into the
object JSON metadata.  The file is intended to be invoked from
``crossing index silhouette score`` and reused by visualizers and later
generation pipelines.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np

from services.silhouette_catalog import scan_catalog


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _bbox_area(bbox: List[int]) -> int:
    if not bbox or len(bbox) < 4:
        return 0
    return max(0, int(bbox[2])) * max(0, int(bbox[3]))


def _composite_for_clip(png_path: Path):
    try:
        from PIL import Image
    except Exception:
        return None

    try:
        img = Image.open(str(png_path)).convert("RGBA")
    except Exception:
        return None

    bg = Image.new("RGB", img.size, (128, 128, 128))
    bg.paste(img.convert("RGB"), mask=img.split()[-1])
    return bg


def compute_scores_for_catalog(
    project_path: str,
    media_type: str = "movies",
    label: Optional[str] = None,
    field: Optional[str] = None,
    rebuild: bool = False,
    verbose: bool = False,
    model_name: Optional[str] = None,
) -> dict:
    """Scan the catalog, compute scores, and persist them into JSON files.

    Returns a summary dict with keys: processed, skipped, errors.
    """
    from tool import prefs
    from services.frame_match import _load_clip_model, clip_score_batch

    records = scan_catalog(project_path, media_type=media_type, label=label)
    if field is not None:
        records = [r for r in records if (r.get("field") or "") == field]

    if not records:
        return {"processed": 0, "skipped": 0, "errors": 0}

    # Load CLIP model used for semantic_label / semantic_field scoring
    clip_model = clip_processor = clip_device = None
    model_choice = model_name or prefs.get("model_frame_match", "clip-vit-base-patch32")
    try:
        clip_model, clip_processor, clip_device = _load_clip_model(project_path, model_choice)
    except Exception:
        if verbose:
            print("Warning: failed to load CLIP model; semantic scores will be 0")
        clip_model = clip_processor = clip_device = None

    processed = skipped = errors = 0

    # Group by frame to compute a simple IoU-based overlap penalty
    groups = {}
    for r in records:
        key = (r.get("filename_stem") or r.get("filename") or "", int(r.get("frame") or -1))
        groups.setdefault(key, []).append(r)

    for rec in records:
        try:
            json_path = Path(rec.get("path"))
            if not json_path.exists():
                errors += 1
                continue

            meta = json.loads(json_path.read_text(encoding="utf-8"))

            # Skip existing scores unless rebuild requested
            if not rebuild and meta.get("usefulness_score") is not None:
                skipped += 1
                continue

            # Load PNG and mask
            png_path = json_path.with_suffix(".png")
            if not png_path.exists():
                errors += 1
                continue

            from PIL import Image
            import cv2

            img_rgba = Image.open(str(png_path)).convert("RGBA")
            arr = np.array(img_rgba)
            if arr.ndim == 3 and arr.shape[2] == 4:
                mask = arr[:, :, 3] > 0
            else:
                mask = np.ones(arr.shape[:2], dtype=bool)

            mask_area = float(mask.sum())

            # Geometry
            frame_w, frame_h = (meta.get("frame_size") or [0, 0])
            frame_area = float(max(1, frame_w * frame_h))
            bbox = meta.get("bbox") or []
            bbox_area = float(_bbox_area(bbox) or 1)

            # fullness_score: mask area / bbox area
            fullness = _clamp01(mask_area / bbox_area)

            # size_score: area fraction mapped
            area_frac = mask_area / frame_area
            size_score = (area_frac - 0.002) / max(1e-9, (0.30 - 0.002))
            size_score = _clamp01(size_score)

            # border_touch: coarse bbox-based check
            touches = False
            if len(bbox) >= 4 and frame_w and frame_h:
                bx, by, bw, bh = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
                border_px = 3
                if bx <= border_px or by <= border_px or (bx + bw) >= (frame_w - border_px) or (by + bh) >= (frame_h - border_px):
                    touches = True
            border_touch_score = 0.0 if touches else 1.0

            # overlap_score: 1 - max IoU with other objects in same frame
            key = (meta.get("filename_stem") or meta.get("filename") or "", int(meta.get("frame") or -1))
            max_iou = 0.0
            for other in groups.get(key, []):
                if other is rec:
                    continue
                other_bbox = other.get("bbox") or []
                if len(other_bbox) >= 4 and len(bbox) >= 4:
                    # IoU
                    ax, ay, aw, ah = (int(x) for x in bbox)
                    bx2, by2, bw2, bh2 = (int(x) for x in other_bbox)
                    ix1 = max(ax, bx2)
                    iy1 = max(ay, by2)
                    ix2 = min(ax + aw, bx2 + bw2)
                    iy2 = min(ay + ah, by2 + bh2)
                    if ix2 > ix1 and iy2 > iy1:
                        inter = (ix2 - ix1) * (iy2 - iy1)
                        union = aw * ah + bw2 * bh2 - inter
                        if union > 0:
                            iou = inter / union
                            max_iou = max(max_iou, iou)
            overlap_score = _clamp01(1.0 - max_iou)

            # viewpoint_score: simple aspect ratio heuristic
            if len(bbox) >= 4:
                bw = max(1, int(bbox[2]))
                bh = max(1, int(bbox[3]))
                ar = bw / bh
            else:
                ar = 1.0
            if 0.5 <= ar <= 2.0:
                viewpoint_score = 1.0
            else:
                viewpoint_score = _clamp01(1.0 - abs(ar - 1.25) / 3.0)

            # semantic scores via CLIP (optional)
            semantic_label = 0.0
            semantic_field = 0.0
            if clip_model is not None and clip_processor is not None and clip_device is not None:
                composite = _composite_for_clip(png_path)
                if composite is not None:
                    try:
                        lab = meta.get("label") or ""
                        fld = meta.get("field") or ""
                        if lab:
                            s = clip_score_batch([composite], lab, clip_model, clip_processor, clip_device)[0]
                            semantic_label = _clamp01(s)
                        if fld:
                            s2 = clip_score_batch([composite], fld, clip_model, clip_processor, clip_device)[0]
                            semantic_field = _clamp01(s2)
                    except Exception:
                        semantic_label = semantic_field = 0.0

            # Composite usefulness
            w_full = 0.22
            w_size = 0.25
            w_overlap = 0.18
            w_view = 0.10
            w_slabel = 0.13
            w_sfield = 0.12

            base = (
                w_full * fullness
                + w_size * size_score
                + w_overlap * overlap_score
                + w_view * viewpoint_score
                + w_slabel * semantic_label
                + w_sfield * semantic_field
            )
            denom = w_full + w_size + w_overlap + w_view + w_slabel + w_sfield
            base = base / denom if denom > 0 else 0.0

            penalty = 0.0
            if touches:
                penalty += 0.40
            # simple fragmentation estimate from mask contours
            try:
                contours, _ = cv2.findContours((mask.astype(np.uint8) * 255), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                frag = max(0, len(contours) - 1)
            except Exception:
                frag = 0
            penalty += min(1.0, frag / 3.0) * 0.25

            usefulness = _clamp01(base - penalty)

            # Write back top-level numeric fields into JSON
            meta.update({
                "fullness_score": round(float(fullness), 6),
                "size_score": round(float(size_score), 6),
                "border_touch_score": round(float(border_touch_score), 6),
                "overlap_score": round(float(overlap_score), 6),
                "viewpoint_score": round(float(viewpoint_score), 6),
                "semantic_label_score": round(float(semantic_label), 6),
                "semantic_field_score": round(float(semantic_field), 6),
                "usefulness_score": round(float(usefulness), 6),
                "quality_generated": datetime.now(timezone.utc).isoformat(),
            })

            json_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            processed += 1
            if verbose:
                print(f"Scored: {json_path}  usefulness={usefulness:.3f}")

        except Exception as exc:
            errors += 1
            if verbose:
                print(f"Error scoring {rec.get('path')}: {exc}")

    return {"processed": processed, "skipped": skipped, "errors": errors}
