"""Silhouette Object Catalog — transparent PNG extraction pipeline.

This module implements the catalog-oriented silhouette pipeline:

    frame
        → segmentation (SAM3)
        → isolated objects (all valid masks for concept)
        → transparent PNG per object
        → indexed catalog on disk

Pipeline philosophy
-------------------
- Generate once, save results, index results, visualize results.
- The transparent PNG is the primary asset — not the mask, not the source frame.
- Every extracted PNG can be traced back to: film / shot / frame / label.
- All bad masks are filtered out before saving.

Storage layout
--------------
    <project>/data/silhouettes/catalog/<media_type>/<filename_stem>/<label>/
        object_0001.png   — transparent RGBA PNG (primary asset)
        object_0001.json  — provenance metadata
        object_0002.png
        object_0002.json
        ...

Label is the vocabulary word (e.g. ``horse``, ``cowboy``, ``revolver``).
``<filename_stem>`` is derived from the video filename (no extension).

Metadata schema (object_NNNN.json)
-----------------------------------
{
    "schema_version": "1",
    "media_type":     "movie",
    "filename":       "django_1966.mkv",
    "filename_stem":  "django_1966",
    "media_id":       "tmdb_11969",
    "shot_id":        "tmdb_11969@f001240-f001310",
    "frame":          1275,
    "label":          "horse",
    "field":          "animals",
    "confidence":     0.91,
    "confidence_broad": 0.82,
    "bbox":           [x, y, w, h],
    "mask_area":      12345,
    "frame_size":     [1920, 1080],
    "sam_model":      "sam3",
    "frame_match_model": "clip-vit-base-patch32",
    "source_frame":   "media/frames/best/movies/...",
    "png":            "object_0001.png",
    "timestamp":      "2026-...",
}
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CATALOG_VERSION = "1"

# Quality filters — shared with services.silhouette but duplicated here
# to keep this module self-contained for the catalog pipeline.
_MIN_MASK_AREA_FRACTION = 0.002   # 0.2 % of frame (slightly looser than polygon pipeline)
_MAX_MASK_AREA_FRACTION = 0.70    # 70 %
_MAX_ASPECT_RATIO       = 6.0     # reject extremely elongated bboxes
_BORDER_CHECK_PX        = 3       # pixels from each edge considered "touching border"
_CLIP_CONFIDENCE_FLOOR  = 0.20    # minimum tight-crop CLIP score to accept a mask
_IOU_DEDUP_THRESHOLD    = 0.70    # IoU above which two masks are considered duplicates
_MAX_OBJECTS_PER_SHOT   = 8       # cap on accepted objects extracted from a single shot
_PNG_CROP_PAD_PX        = 6       # pixel padding around tight bbox when saving PNG


def _scanned_marker_path(
    project_path: str,
    media_type: str,
    field: str,
    label: str,
) -> Path:
    """Return the path of the sentinel file that marks a (field, label) as fully scanned."""
    safe = _safe_label(label)
    safe_field = re.sub(r"[^a-z0-9_]", "_", field.lower().strip())
    return catalog_base_dir(project_path, media_type) / ".scanned" / safe_field / safe


def is_label_scanned(
    project_path: str,
    media_type: str,
    field: str,
    label: str,
) -> bool:
    """Return True when a corpus-wide scan for (field, label) has already completed."""
    return _scanned_marker_path(project_path, media_type, field, label).exists()


def mark_label_scanned(
    project_path: str,
    media_type: str,
    field: str,
    label: str,
) -> None:
    """Write a sentinel file recording that (field, label) has been fully scanned."""
    p = _scanned_marker_path(project_path, media_type, field, label)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def unmark_label_scanned(
    project_path: str,
    media_type: str,
    field: str,
    label: str,
) -> None:
    """Remove the scanned sentinel for (field, label), e.g. when --force is used."""
    p = _scanned_marker_path(project_path, media_type, field, label)
    if p.exists():
        p.unlink()


def sort_labels(labels: "list[str]") -> "list[str]":
    """Return *labels* sorted case-insensitively (A and a interleaved, not separated).

    Use this wherever a label list is displayed to users — CLI, MCP, and
    visualizers — so the sort order is consistent across all surfaces.
    """
    return sorted(labels, key=str.casefold)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _safe_label(label: str) -> str:
    """Return a filesystem-safe label string (lowercase, spaces → underscores)."""
    return re.sub(r"[^a-z0-9_]", "_", label.lower().strip())


def catalog_base_dir(project_path: str, media_type: str) -> Path:
    """Return the catalog root directory for *media_type*."""
    return Path(project_path) / "data" / "silhouettes" / "catalog" / media_type


def catalog_item_dir(
    project_path: str,
    media_type: str,
    filename_stem: str,
    label: str,
) -> Path:
    """Return the directory where objects for (media_item, label) are stored."""
    return catalog_base_dir(project_path, media_type) / filename_stem / _safe_label(label)


def _next_object_index(label_dir: Path) -> int:
    """Return the next available 1-based object index for *label_dir*.

    Scans for existing ``object_NNNN.png`` files and returns max_index + 1,
    or 1 if the directory is empty.
    """
    if not label_dir.exists():
        return 1
    existing = sorted(label_dir.glob("object_????.png"))
    if not existing:
        return 1
    last = existing[-1]
    try:
        return int(last.stem.split("_")[1]) + 1
    except (IndexError, ValueError):
        return len(existing) + 1


def _object_png_name(index: int) -> str:
    return f"object_{index:04d}.png"


def _object_json_name(index: int) -> str:
    return f"object_{index:04d}.json"


# ---------------------------------------------------------------------------
# PNG extraction from SAM mask
# ---------------------------------------------------------------------------

def extract_object_png(image_pil, mask_dict: dict, pad_px: int = _PNG_CROP_PAD_PX):
    """Extract the masked object as an RGBA image with transparent background.

    Parameters
    ----------
    image_pil:  Full-frame PIL RGB image.
    mask_dict:  SAM mask dict (must have ``"segmentation"`` key).
    pad_px:     Pixel padding around the tight bounding box.

    Returns
    -------
    PIL RGBA image cropped to the object bounding box.
    Returns ``None`` when the mask is empty or extraction fails.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow and numpy are required.\n"
            "Install with: pip install Pillow numpy"
        ) from exc

    seg = np.asarray(mask_dict.get("segmentation"), dtype=bool)
    if seg is None or not seg.any():
        return None

    rows = np.any(seg, axis=1)
    cols = np.any(seg, axis=0)
    row_indices = np.where(rows)[0]
    col_indices = np.where(cols)[0]
    if len(row_indices) == 0 or len(col_indices) == 0:
        return None

    h, w = seg.shape
    rmin = max(0, int(row_indices[0])  - pad_px)
    rmax = min(h - 1, int(row_indices[-1]) + pad_px)
    cmin = max(0, int(col_indices[0])  - pad_px)
    cmax = min(w - 1, int(col_indices[-1]) + pad_px)

    img_arr  = np.array(image_pil, dtype=np.uint8)
    img_crop  = img_arr[rmin:rmax + 1, cmin:cmax + 1]
    mask_crop = seg[rmin:rmax + 1, cmin:cmax + 1]

    rgba = np.zeros((*img_crop.shape[:2], 4), dtype=np.uint8)
    rgba[:, :, :3] = img_crop
    rgba[:, :, 3]  = (mask_crop * 255).astype(np.uint8)

    return Image.fromarray(rgba, mode="RGBA")


# ---------------------------------------------------------------------------
# Mask quality filters
# ---------------------------------------------------------------------------

def _passes_quality_filters(
    mask_dict: dict,
    frame_area: int,
) -> tuple[bool, str]:
    """Return (passes, reason_if_rejected) for a SAM mask dict.

    Checks area fraction and bounding-box aspect ratio.
    Does NOT check CLIP score (caller handles that separately).
    """
    area = mask_dict.get("area", 0)
    if area < frame_area * _MIN_MASK_AREA_FRACTION:
        return False, f"mask too small ({area} px < {frame_area * _MIN_MASK_AREA_FRACTION:.0f})"
    if area > frame_area * _MAX_MASK_AREA_FRACTION:
        return False, f"mask too large ({area} px > {frame_area * _MAX_MASK_AREA_FRACTION:.0f})"
    bbox = mask_dict.get("bbox", [])
    if len(bbox) >= 4:
        bw, bh = float(bbox[2]), float(bbox[3])
        if bw > 0 and bh > 0 and max(bw, bh) / min(bw, bh) > _MAX_ASPECT_RATIO:
            return False, f"aspect ratio too extreme ({max(bw,bh)/min(bw,bh):.1f} > {_MAX_ASPECT_RATIO})"
    return True, "ok"


def _touches_border(mask_dict: dict, frame_h: int, frame_w: int, border_px: int = _BORDER_CHECK_PX) -> bool:
    """Return True when the mask's segmentation touches the frame border."""
    import numpy as np

    seg = mask_dict.get("segmentation")
    if seg is None:
        return True
    m = np.asarray(seg, dtype=bool)
    if m.ndim != 2:
        return True
    return bool(
        m[:border_px, :].any()
        or m[-border_px:, :].any()
        or m[:, :border_px].any()
        or m[:, -border_px:].any()
    )


def _masks_iou(seg_a, seg_b) -> float:
    """Compute intersection-over-union between two boolean mask arrays."""
    import numpy as np

    a = np.asarray(seg_a, dtype=bool)
    b = np.asarray(seg_b, dtype=bool)
    inter = int((a & b).sum())
    union = int((a | b).sum())
    return inter / union if union > 0 else 0.0


def _deduplicate_masks(
    ranked: list[tuple[dict, float]],
    iou_threshold: float = _IOU_DEDUP_THRESHOLD,
) -> list[tuple[dict, float]]:
    """Remove near-duplicate masks (IoU > threshold).

    Keeps the highest-scoring mask in each cluster.  The input list must be
    sorted by score descending.
    """
    import numpy as np

    accepted: list[tuple[dict, float]] = []
    accepted_segs = []

    for mask_dict, score in ranked:
        seg = np.asarray(mask_dict.get("segmentation"), dtype=bool)
        is_dup = any(_masks_iou(seg, s) > iou_threshold for s in accepted_segs)
        if not is_dup:
            accepted.append((mask_dict, score))
            accepted_segs.append(seg)

    return accepted


# ---------------------------------------------------------------------------
# Per-shot object extraction
# ---------------------------------------------------------------------------

def extract_objects_for_shot(
    project_path: str,
    label: str,
    field: str,
    shot_id: str,
    filename: str,
    media_id: str,
    media_type: str,
    sam_model_name: str,
    frame_model_name: str,
    *,
    clip_model=None,
    clip_processor=None,
    clip_device=None,
    mask_generator=None,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Extract all valid objects for *label* from one shot.

    Runs CLIP best-frame selection then SAM segmentation, filters all masks
    by quality and CLIP confidence, deduplicates by IoU, and saves each
    accepted object as a transparent PNG + JSON metadata file.

    Parameters
    ----------
    label:           Vocabulary word / object category (e.g. ``"horse"``).
    field:           Annotation field (e.g. ``"animals"``); used for metadata.
    shot_id:         Canonical shot identifier (``<media_id>@fSTART-fEND``).
    filename:        Video filename (e.g. ``"django_1966.mkv"``).
    media_id:        Media identifier (e.g. ``"tmdb_11969"``).
    force:           Overwrite existing objects for this shot.
    verbose:         Print progress detail.

    Returns
    -------
    dict with keys:
      ``saved``      — list of saved PNG paths (str)
      ``skipped``    — number of masks skipped (quality / confidence / dedup)
      ``reason``     — short description of outcome
    """
    import numpy as np

    from data.media_id import parse_shot_id
    from services.silhouette import (
        extract_frame_as_pil,
        rank_masks_by_clip,
        rerank_by_tight_crop,
        _RERANK_TOP_N,
    )
    from services.frame_match import (
        _get_video_fps,
        _load_clip_model,
        find_query_best_frame_for_shot,
        best_frame_path,
    )

    filename_stem = Path(filename).stem
    label_dir = catalog_item_dir(project_path, media_type, filename_stem, label)

    # --- check if already done for this shot (unless --force) ---
    if not force and label_dir.exists():
        existing = list(label_dir.glob("object_????.json"))
        for jf in existing:
            try:
                meta = json.loads(jf.read_text(encoding="utf-8"))
                if meta.get("shot_id") == shot_id:
                    if verbose:
                        print(f"    (cached) {jf.name} already exists for {shot_id}")
                    return {"saved": [], "skipped": 0, "reason": "cached"}
            except Exception:
                pass

    # --- resolve video path ---
    video_path = Path(project_path) / "media" / "videos" / media_type / filename
    if not video_path.exists():
        return {"saved": [], "skipped": 0, "reason": f"video not found: {video_path}"}

    # --- parse shot boundaries ---
    try:
        _, start_frame, end_frame = parse_shot_id(shot_id)
    except ValueError as exc:
        return {"saved": [], "skipped": 0, "reason": f"invalid shot_id: {exc}"}

    fps = _get_video_fps(str(video_path))

    # --- load CLIP model if not pre-loaded ---
    if clip_model is None:
        if verbose:
            print(f"    Loading CLIP '{frame_model_name}'…")
        try:
            clip_model, clip_processor, clip_device = _load_clip_model(
                project_path, frame_model_name
            )
        except (ImportError, RuntimeError) as exc:
            return {"saved": [], "skipped": 0, "reason": f"CLIP load failed: {exc}"}

    # --- find best frame for label in this shot ---
    if verbose:
        print(f"    Selecting best frame for '{label}' in {shot_id}…")
    try:
        frame_index, _frame_score = find_query_best_frame_for_shot(
            project_path=project_path,
            filename=filename,
            shot_id=shot_id,
            query=label,
            media_type=media_type,
            model=clip_model,
            processor=clip_processor,
            device=clip_device,
        )
    except Exception as exc:
        return {"saved": [], "skipped": 0, "reason": f"frame selection failed: {exc}"}

    # --- extract frame image ---
    image_pil = extract_frame_as_pil(str(video_path), frame_index, fps)
    if image_pil is None:
        return {"saved": [], "skipped": 0, "reason": f"frame extraction failed at {frame_index}"}

    frame_w, frame_h = image_pil.size
    frame_area = frame_w * frame_h

    # --- load SAM3 model if not pre-loaded ---
    if mask_generator is None:
        from services.silhouette import load_sam_model
        if verbose:
            print(f"    Loading SAM3 '{sam_model_name}'…")
        try:
            mask_generator, sam_model_name, _ = load_sam_model(project_path, sam_model_name)
        except (ImportError, FileNotFoundError, RuntimeError) as exc:
            return {"saved": [], "skipped": 0, "reason": f"SAM3 load failed: {exc}"}

    # --- run SAM3 concept segmentation ---
    if verbose:
        print(f"    Running SAM3 concept segmentation for '{label}' (frame {frame_index})…")
    try:
        masks = mask_generator.segment_concept(image_pil, label)
    except Exception as exc:
        return {"saved": [], "skipped": 0, "reason": f"SAM3 segmentation failed: {exc}"}

    if not masks:
        return {"saved": [], "skipped": 0, "reason": "SAM3 produced no masks"}

    # --- quality pre-filter ---
    quality_passed: list[dict] = []
    skipped_quality = 0
    for m in masks:
        ok, _why = _passes_quality_filters(m, frame_area)
        if ok and not _touches_border(m, frame_h, frame_w):
            quality_passed.append(m)
        else:
            skipped_quality += 1

    if not quality_passed:
        return {
            "saved": [],
            "skipped": skipped_quality,
            "reason": f"all {len(masks)} masks failed quality filters",
        }

    # --- broad CLIP ranking ---
    from services.silhouette import rank_masks_by_clip, _CLIP_SCORE_FLOOR

    ranked_broad = rank_masks_by_clip(
        quality_passed,
        image_pil,
        label,
        clip_model,
        clip_processor,
        clip_device,
    )

    if not ranked_broad:
        return {"saved": [], "skipped": skipped_quality, "reason": "no masks after CLIP ranking"}

    # --- tight-crop re-scoring (take top-N broad candidates) ---
    top_n = ranked_broad[:_RERANK_TOP_N]
    reranked = rerank_by_tight_crop(top_n, image_pil, label, clip_model, clip_processor, clip_device)

    # Filter by confidence floor
    confident = [(m, broad, tight) for m, broad, tight in reranked if tight >= _CLIP_CONFIDENCE_FLOOR]
    skipped_confidence = len(reranked) - len(confident)

    if not confident:
        return {
            "saved": [],
            "skipped": skipped_quality + skipped_confidence,
            "reason": (
                f"no masks above confidence floor {_CLIP_SCORE_FLOOR} "
                f"(best tight score: {reranked[0][2]:.3f} for {len(reranked)} candidates)"
                if reranked else "no candidates after re-ranking"
            ),
        }

    # Convert to (mask_dict, tight_score) for deduplication
    confident_pairs = [(m, tight) for m, _broad, tight in confident]

    # --- IoU deduplication ---
    deduped = _deduplicate_masks(confident_pairs, _IOU_DEDUP_THRESHOLD)

    # Cap at max objects per shot
    deduped = deduped[:_MAX_OBJECTS_PER_SHOT]

    # --- build source frame reference ---
    bf_path = best_frame_path(project_path, media_type, filename, shot_id)
    source_frame = str(bf_path) if bf_path.exists() else f"frame:{frame_index}"

    # --- save accepted objects ---
    label_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    next_idx = _next_object_index(label_dir)
    timestamp = datetime.now(timezone.utc).isoformat()

    # Load semantic analysis prompts once for this shot (best-effort).
    # A missing or malformed prompt file must never block extraction.
    _sem_prompts = None
    try:
        from services.silhouette_prompt import load_silhouette_prompts
        _, _sem_prompts = load_silhouette_prompts(project_path)
    except Exception:
        pass

    for mask_dict, tight_score in deduped:
        # Find broad score for this mask
        broad_score = next(
            (bs for m, bs, ts in reranked if m is mask_dict),
            0.0,
        )

        png_img = extract_object_png(image_pil, mask_dict)
        if png_img is None:
            continue

        idx = next_idx
        next_idx += 1

        png_name  = _object_png_name(idx)
        json_name = _object_json_name(idx)

        png_path  = label_dir / png_name
        json_path = label_dir / json_name

        # Save PNG
        png_img.save(str(png_path), format="PNG", optimize=False)

        # Build metadata
        bbox = [int(v) for v in mask_dict.get("bbox", [])]
        meta: dict[str, Any] = {
            "schema_version":     CATALOG_VERSION,
            "media_type":         media_type,
            "filename":           filename,
            "filename_stem":      filename_stem,
            "media_id":           media_id,
            "shot_id":            shot_id,
            "frame":              frame_index,
            "label":              label,
            "field":              field,
            "confidence":         round(float(tight_score), 6),
            "confidence_broad":   round(float(broad_score), 6),
            "bbox":               bbox,
            "mask_area":          int(mask_dict.get("area", 0)),
            "frame_size":         [frame_w, frame_h],
            "sam_model":          sam_model_name,
            "frame_match_model":  frame_model_name,
            "source_frame":       source_frame,
            "png":                png_name,
            "timestamp":          timestamp,
        }
        json_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Inline semantic enrichment — uses the already-loaded CLIP model so
        # there is no extra model load.  Best-effort: any failure is logged
        # (when --verbose) but never prevents the PNG from being saved.
        try:
            from services.silhouette_semantics import analyze_silhouette_semantics
            sem = analyze_silhouette_semantics(
                png_path,
                meta,
                clip_model=clip_model,
                clip_processor=clip_processor,
                clip_device=clip_device,
                prompts=_sem_prompts,
                project_path=project_path,
            )
            meta.update(sem)
            json_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as _sem_exc:
            if verbose:
                print(f"    (semantic enrichment skipped: {_sem_exc})")

        saved_paths.append(str(png_path))
        if verbose:
            print(f"    ✓ {png_name}  confidence={tight_score:.3f}  bbox={bbox}")

    if not saved_paths:
        return {"saved": [], "skipped": skipped_quality + skipped_confidence, "reason": "PNG extraction failed for all masks"}

    return {
        "saved": saved_paths,
        "skipped": skipped_quality + skipped_confidence + (len(deduped) - len(saved_paths)),
        "reason": "ok",
    }


# ---------------------------------------------------------------------------
# Candidate search (reuses search infrastructure)
# ---------------------------------------------------------------------------

def _find_shot_candidates(
    project_path: str,
    label: str,
    field: str,
    media_type: str,
    scope_type: str,
    scope_value: str | None,
    limit: int = 200,
    limit_per_item: int = 10,
) -> list[dict]:
    """Return candidate shots containing *label* in *field* via search index."""
    from services.search import search_shots

    use_all = scope_type == "all"
    scopes = [scope_value] if (scope_type == "movie" and scope_value) else None

    result = search_shots(
        query=label,
        scopes=scopes,
        field=field,
        limit=limit,
        limit_per_item=limit_per_item,
        use_all=use_all,
        project_path=project_path,
        media_type=media_type,
    )
    return result.get("results", [])


# ---------------------------------------------------------------------------
# Per-movie extraction
# ---------------------------------------------------------------------------

def extract_catalog_for_movie(
    project_path: str,
    filename: str,
    media_id: str,
    label: str,
    field: str,
    media_type: str = "movie",
    sam_model_name: str = "sam3.pt",
    frame_model_name: str = "clip-vit-base-patch32",
    force: bool = False,
    verbose: bool = False,
    *,
    clip_model=None,
    clip_processor=None,
    clip_device=None,
    mask_generator=None,
) -> dict:
    """Extract all catalog objects for *label* from one media item.

    Returns a summary dict:
      ``total_shots``  — number of candidate shots found
      ``shots_saved``  — shots that yielded at least one PNG
      ``total_saved``  — total PNG files written
      ``total_skipped``— total masks discarded
      ``failed``       — number of shots that hit an error
    """
    if verbose:
        print(f"  [{filename}] Searching for '{label}' candidates…")

    candidates = _find_shot_candidates(
        project_path=project_path,
        label=label,
        field=field,
        media_type=media_type,
        scope_type="movie",
        scope_value=filename,  # filename is unambiguously matched by the search resolver
    )

    if not candidates:
        if verbose:
            print(f"  [{filename}] No candidates found for '{label}'.")
        return {
            "total_shots": 0,
            "shots_saved": 0,
            "total_saved": 0,
            "total_skipped": 0,
            "failed": 0,
        }

    if verbose:
        print(f"  [{filename}] {len(candidates)} candidate shot(s) for '{label}'.")

    # Pre-load CLIP once if not already provided, so all shots share one load
    if clip_model is None:
        from services.frame_match import _load_clip_model
        if verbose:
            print(f"  Loading CLIP model '{frame_model_name}'…")
        try:
            clip_model, clip_processor, clip_device = _load_clip_model(
                project_path, frame_model_name
            )
        except (ImportError, RuntimeError) as exc:
            raise RuntimeError(f"CLIP model load failed: {exc}") from exc

    total_saved  = 0
    shots_saved  = 0
    total_skipped = 0
    failed        = 0

    for candidate in candidates:
        shot_id        = candidate.get("shot_id", "")
        cand_filename  = candidate.get("filename") or filename
        if not shot_id:
            continue

        if verbose:
            score = candidate.get("score", 0)
            print(f"  [shot {shot_id}] score={score:.3f}")

        try:
            result = extract_objects_for_shot(
                project_path=project_path,
                label=label,
                field=field,
                shot_id=shot_id,
                filename=cand_filename,
                media_id=media_id,
                media_type=media_type,
                sam_model_name=sam_model_name,
                frame_model_name=frame_model_name,
                clip_model=clip_model,
                clip_processor=clip_processor,
                clip_device=clip_device,
                mask_generator=mask_generator,
                force=force,
                verbose=verbose,
            )
        except Exception as exc:
            if verbose:
                print(f"  [shot {shot_id}] ✗ {exc}")
            failed += 1
            continue

        n_saved = len(result.get("saved", []))
        total_saved   += n_saved
        total_skipped += result.get("skipped", 0)
        if n_saved > 0:
            shots_saved += 1
        elif result.get("reason") != "cached" and verbose:
            print(f"  [shot {shot_id}] no objects extracted: {result.get('reason')}")

    return {
        "total_shots":   len(candidates),
        "shots_saved":   shots_saved,
        "total_saved":   total_saved,
        "total_skipped": total_skipped,
        "failed":        failed,
    }


# ---------------------------------------------------------------------------
# Corpus-wide extraction
# ---------------------------------------------------------------------------

def extract_catalog_for_all(
    project_path: str,
    label: str,
    field: str,
    media_type: str = "movie",
    sam_model_name: str = "sam3.pt",
    frame_model_name: str = "clip-vit-base-patch32",
    force: bool = False,
    verbose: bool = False,
    on_item_done=None,
) -> dict:
    """Extract catalog objects for *label* across the entire corpus.

    *on_item_done* is an optional callback ``(filename, summary, exc)`` called
    after each media item finishes (similar to palette and motif batch API).

    Returns an aggregate summary dict:
      ``total_files``     — number of media items processed
      ``total_shots``     — total candidate shots across all items
      ``total_saved``     — total PNG files written
      ``total_skipped``   — total masks discarded
      ``total_failed``    — total shot-level errors
    """
    from data.metadata import get_metadata
    from data.media_id import compute_media_id
    from services.silhouette import load_sam_model
    from services.frame_match import _load_clip_model

    entries = get_metadata(project_path, media_type=media_type)
    if not entries:
        return {
            "total_files": 0,
            "total_shots": 0,
            "total_saved": 0,
            "total_skipped": 0,
            "total_failed": 0,
        }

    # Pre-load models once for all items
    if verbose:
        print(f"Loading CLIP model '{frame_model_name}'…")
    try:
        clip_model, clip_processor, clip_device = _load_clip_model(
            project_path, frame_model_name
        )
    except (ImportError, RuntimeError) as exc:
        raise RuntimeError(f"CLIP model load failed: {exc}") from exc

    if verbose:
        print(f"Loading SAM3 model '{sam_model_name}'…")
    try:
        mask_generator, effective_sam_name, _ = load_sam_model(project_path, sam_model_name)
    except (ImportError, FileNotFoundError, RuntimeError) as exc:
        raise RuntimeError(f"SAM3 model load failed: {exc}") from exc

    total_files  = 0
    total_shots  = 0
    total_saved  = 0
    total_skipped = 0
    total_failed  = 0

    for entry in entries:
        filename = entry.get("filename")
        if not filename:
            continue

        media_id = compute_media_id(entry, media_type)
        total_files += 1

        exc_caught = None
        summary: dict = {}
        try:
            summary = extract_catalog_for_movie(
                project_path=project_path,
                filename=filename,
                media_id=media_id,
                label=label,
                field=field,
                media_type=media_type,
                sam_model_name=effective_sam_name,
                frame_model_name=frame_model_name,
                force=force,
                verbose=verbose,
                clip_model=clip_model,
                clip_processor=clip_processor,
                clip_device=clip_device,
                mask_generator=mask_generator,
            )
            total_shots   += summary.get("total_shots", 0)
            total_saved   += summary.get("total_saved", 0)
            total_skipped += summary.get("total_skipped", 0)
            total_failed  += summary.get("failed", 0)
        except Exception as exc:
            exc_caught = exc
            if verbose:
                print(f"  [{filename}] ✗ {exc}")
            total_failed += 1

        if on_item_done is not None:
            try:
                on_item_done(filename, summary, exc_caught)
            except Exception:
                pass

    # Mark this (field, label) pair as fully scanned so future runs can skip it.
    # Only mark when there were no failures (a partial run should be retried).
    if total_failed == 0:
        mark_label_scanned(project_path, media_type, field, label)

    return {
        "total_files":   total_files,
        "total_shots":   total_shots,
        "total_saved":   total_saved,
        "total_skipped": total_skipped,
        "total_failed":  total_failed,
    }


# ---------------------------------------------------------------------------
# Catalog scanning
# ---------------------------------------------------------------------------

def scan_catalog(
    project_path: str,
    media_type: str = "movie",
    label: str | None = None,
    filename_stem: str | None = None,
) -> list[dict]:
    """Return a flat list of all catalog entries (one dict per object JSON).

    Each dict contains the full metadata from the JSON file plus a ``path``
    key (Path to the JSON file).

    Pass *label* to restrict to one label.
    Pass *filename_stem* to restrict to one media item.
    """
    base = catalog_base_dir(project_path, media_type)
    records: list[dict] = []

    if not base.exists():
        return records

    item_dirs = sorted(base.iterdir()) if base.exists() else []
    for item_dir in item_dirs:
        if not item_dir.is_dir():
            continue
        if filename_stem is not None and item_dir.name != filename_stem:
            continue

        for label_dir in sorted(item_dir.iterdir()):
            if not label_dir.is_dir():
                continue
            if label is not None and label_dir.name != _safe_label(label):
                continue

            for json_file in sorted(label_dir.glob("object_????.json")):
                try:
                    meta = json.loads(json_file.read_text(encoding="utf-8"))
                    meta["path"] = json_file
                    records.append(meta)
                except Exception:
                    records.append({"path": json_file, "error": "unreadable"})

    return records


def backfill_scanned_from_catalog(
    project_path: str,
    media_type: str = "movie",
) -> dict:
    """Write scanned sentinels for every (field, label) pair that has existing
    catalog entries on disk.

    This is a one-time recovery tool for runs started before sentinel tracking
    was introduced.  It cannot mark labels that were scanned but found no
    results — those will be re-run on the next ``--fields`` pass (fast: text
    search only, no GPU).

    Returns a dict:
      ``written``  — sentinels newly written
      ``already``  — sentinels that already existed
      ``pairs``    — list of (field, label) pairs found
    """
    base = catalog_base_dir(project_path, media_type)
    if not base.exists():
        return {"written": 0, "already": 0, "pairs": []}

    # Collect unique (field, label) pairs from all object JSON files.
    # Read only the first JSON per label-dir to get field+label cheaply.
    pairs: set[tuple[str, str]] = set()

    for item_dir in base.iterdir():
        if not item_dir.is_dir() or item_dir.name.startswith("."):
            continue
        for label_dir in item_dir.iterdir():
            if not label_dir.is_dir():
                continue
            # Read first available JSON to get field + label
            for json_file in sorted(label_dir.glob("object_????.json")):
                try:
                    meta = json.loads(json_file.read_text(encoding="utf-8"))
                    fld = meta.get("field")
                    lbl = meta.get("label")
                    if fld and lbl:
                        pairs.add((fld, lbl))
                except Exception:
                    pass
                break  # only need one file per label_dir

    written = 0
    already = 0
    for fld, lbl in sorted(pairs):
        if is_label_scanned(project_path, media_type, fld, lbl):
            already += 1
        else:
            mark_label_scanned(project_path, media_type, fld, lbl)
            written += 1

    return {"written": written, "already": already, "pairs": sorted(pairs)}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def audit_catalog(
    project_path: str,
    media_type: str = "movie",
    label: str | None = None,
    filename_stem: str | None = None,
) -> dict:
    """Return a summary of the catalog state.

    Returns a dict:
      ``total_objects``    — total PNG/JSON pairs found
      ``labels``           — {label: count} mapping
      ``media_items``      — {filename_stem: count} mapping
      ``by_label``         — sorted list of (label, count) tuples
      ``errors``           — number of unreadable JSON files
    """
    records = scan_catalog(
        project_path, media_type=media_type, label=label, filename_stem=filename_stem
    )

    labels: dict[str, int] = {}
    items: dict[str, int]  = {}
    errors = 0

    for r in records:
        if "error" in r:
            errors += 1
            continue
        lbl  = r.get("label", r.get("path").parent.name if isinstance(r.get("path"), Path) else "?")
        stem = r.get("filename_stem", "?")
        labels[lbl]  = labels.get(lbl, 0) + 1
        items[stem]  = items.get(stem, 0) + 1

    return {
        "total_objects": len(records) - errors,
        "labels":        labels,
        "media_items":   items,
        "by_label":      sorted(labels.items(), key=lambda t: -t[1]),
        "errors":        errors,
    }


# ---------------------------------------------------------------------------
# Clear / delete
# ---------------------------------------------------------------------------

def clear_catalog(
    project_path: str,
    media_type: str = "movie",
    label: str | None = None,
    filename_stem: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Delete catalog entries matching the given filters.

    If both *label* and *filename_stem* are None, the entire catalog for
    *media_type* is cleared (requires confirmation from the caller; this
    function does not prompt).

    Returns:
      ``deleted_files``  — number of files (PNG + JSON) removed
      ``deleted_dirs``   — number of now-empty directories removed
      ``dry_run``        — True when no files were actually deleted
    """
    base = catalog_base_dir(project_path, media_type)
    if not base.exists():
        return {"deleted_files": 0, "deleted_dirs": 0, "dry_run": dry_run}

    deleted_files = 0
    deleted_dirs  = 0

    if label is None and filename_stem is None:
        # Clear entire media_type catalog
        all_files = list(base.rglob("object_????.*"))
        deleted_files = len(all_files)
        if not dry_run:
            shutil.rmtree(str(base), ignore_errors=True)
    else:
        # Targeted deletion
        for item_dir in sorted(base.iterdir()):
            if not item_dir.is_dir():
                continue
            if filename_stem is not None and item_dir.name != filename_stem:
                continue

            for label_dir in sorted(item_dir.iterdir()):
                if not label_dir.is_dir():
                    continue
                if label is not None and label_dir.name != _safe_label(label):
                    continue

                targets = list(label_dir.glob("object_????.*"))
                deleted_files += len(targets)
                if not dry_run:
                    for t in targets:
                        t.unlink(missing_ok=True)
                    # Remove empty label dir
                    remaining = list(label_dir.iterdir())
                    if not remaining:
                        label_dir.rmdir()
                        deleted_dirs += 1

            # Remove empty item dir
            if not dry_run and item_dir.exists():
                if not any(item_dir.iterdir()):
                    item_dir.rmdir()
                    deleted_dirs += 1

    return {
        "deleted_files": deleted_files,
        "deleted_dirs":  deleted_dirs,
        "dry_run":       dry_run,
    }
