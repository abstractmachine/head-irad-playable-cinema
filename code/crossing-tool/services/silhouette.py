"""Silhouette extraction and caching for Crossing vocabulary words.

This module implements a CLIP + SAM pipeline that finds the best silhouette
polygon for a single vocabulary word in a single annotation field/category.

Pipeline
--------
1. Resolve the word + field + scope into candidate shots (via search_shots).
2. Rank candidates by relevance score (highest first).
3. For each candidate:
   a. Extract the best frame for the *word* query using CLIP.
   b. Run SAM automatic mask generation to get candidate regions.
   c. Score each mask crop against the word using CLIP.
   d. Check containment: reject masks that touch any frame edge.
   e. Convert the accepted mask to a polygon and simplify it.
4. Write the accepted result as a canonical JSON file to the silhouette cache.

Storage
-------
Cache files live under:
    <project>/data/silhouettes/<media_type>/<scope>/<field>/<word>/

Filename format:
    best__src-<media_id>__shot-<shot_id>__frame-<frame_index>__score-<score>__v1.json

Score encoding: decimal point replaced by "p" so 0.942 → "0p942".
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Imported here so tests can patch ``services.silhouette.compute_media_id``
# and ``services.silhouette.get_metadata`` without touching the source modules.
from data.media_id import compute_media_id, parse_shot_id
from data.metadata import get_metadata

# ---------------------------------------------------------------------------
# Cache path helpers
# ---------------------------------------------------------------------------

SILHOUETTE_VERSION = "v1"
_BORDER_CHECK_PX = 3          # pixels from each edge considered "touching border"
_MIN_MASK_AREA_FRACTION = 0.003   # smallest acceptable mask (0.3% of frame area)
_MAX_MASK_AREA_FRACTION = 0.75    # largest acceptable mask (75% of frame area)
_MIN_POLYGON_POINTS = 3

# Acceptance thresholds — prefer false negatives over false positives
_CLIP_SCORE_FLOOR = 0.20       # absolute minimum CLIP score for any accepted mask
_CLIP_RUNNER_UP_MARGIN = 0.025 # top mask must beat runner-up by at least this much

# Two-stage verification parameters
_RERANK_TOP_N = 5              # top broad candidates fed into tight-crop re-scoring
_TIGHT_CROP_PAD_FRACTION = 0.02  # 2% padding for tight-crop pass (vs 10% for broad)
_MAX_ASPECT_RATIO = 6.0        # reject bbox with max/min side ratio above this (landscape filter)


def _safe_word(word: str) -> str:
    """Return a filesystem-safe version of *word* (lowercase, spaces → underscores)."""
    return re.sub(r"[^a-z0-9_]", "_", word.lower().strip())


def _safe_field(field: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", field.lower().strip())


def _score_to_str(score: float) -> str:
    """Encode a float score as a filename-safe string (e.g. 0.942 → '0p942')."""
    return f"{score:.3f}".replace(".", "p")


def silhouette_cache_dir(
    project_path: str,
    media_type: str,
    scope: str,
    field: str,
    word: str,
) -> Path:
    """Return the cache directory for a (scope, field, word) combination.

    Scope conventions:
      - ``"all"``               — full corpus
      - ``"movie-<media_id>"``  — single movie
      - ``"shot-<shot_id>"``    — single shot (diagnostic mode)
    """
    return (
        Path(project_path)
        / "data" / "silhouettes"
        / media_type / scope
        / _safe_field(field) / _safe_word(word)
    )


def silhouette_filename(
    media_id: str,
    shot_id: str,
    frame_index: int,
    score: float,
) -> str:
    """Return the canonical JSON filename for a silhouette result."""
    score_str = _score_to_str(score)
    frame_str = f"{frame_index:06d}"
    return (
        f"best__src-{media_id}__shot-{shot_id}"
        f"__frame-{frame_str}__score-{score_str}__{SILHOUETTE_VERSION}.json"
    )


def silhouette_json_path(
    project_path: str,
    media_type: str,
    scope: str,
    field: str,
    word: str,
    media_id: str,
    shot_id: str,
    frame_index: int,
    score: float,
) -> Path:
    """Return the full Path for a silhouette JSON file."""
    cache_dir = silhouette_cache_dir(project_path, media_type, scope, field, word)
    filename = silhouette_filename(media_id, shot_id, frame_index, score)
    return cache_dir / filename


def silhouette_exists(
    project_path: str,
    media_type: str,
    scope: str,
    field: str,
    word: str,
) -> tuple[bool, Path | None]:
    """Check whether a cached silhouette already exists for this combination.

    Returns ``(True, path)`` when exactly one JSON file is found,
    ``(False, None)`` otherwise.  Presence of multiple files is treated as
    "exists" (returns the lexicographically last one).
    """
    cache_dir = silhouette_cache_dir(project_path, media_type, scope, field, word)
    if not cache_dir.exists():
        return False, None
    candidates = sorted(cache_dir.glob(f"best__*__{SILHOUETTE_VERSION}.json"))
    if not candidates:
        return False, None
    return True, candidates[-1]


def load_silhouette(path: Path) -> dict | None:
    """Load a silhouette JSON file. Returns None on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _score_from_path(path: Path) -> float:
    """Parse the __score-<value>__ segment from a silhouette filename.

    e.g. ``best__src-tmdb_1__shot-....__score-0p942__v1.json`` → 0.942
    """
    try:
        for part in path.stem.split("__"):
            if part.startswith("score-"):
                return float(part[6:].replace("p", "."))
    except (ValueError, IndexError):
        pass
    return 0.0


def scan_records_flat(
    project_path: str,
    media_type: str,
    filter_field: str | None = None,
) -> list[dict]:
    """Scan the silhouette cache and return a flat list of all cached records.

    Returns one dict per JSON file (not deduplicated by word).  Useful for
    the visualizer and for enumerating all cached silhouettes.

    Each record dict:
    - ``path``   (Path)  — absolute path to the JSON file
    - ``field``  (str)   — annotation field / category
    - ``word``   (str)   — vocabulary word
    - ``scope``  (str)   — cache scope directory name (e.g. ``"all"``, ``"movie-tmdb_11969"``)
    - ``score``  (float) — CLIP score parsed from the filename

    Sorted by ``(field, word, scope, score descending)``.
    Pass *filter_field* to restrict to one field.
    """
    base = Path(project_path) / "data" / "silhouettes" / media_type
    records: list[dict] = []

    if not base.exists():
        return records

    for scope_dir in sorted(base.iterdir()):
        if not scope_dir.is_dir() or scope_dir.name == "manifest.json":
            continue
        scope = scope_dir.name
        for field_dir in sorted(scope_dir.iterdir()):
            if not field_dir.is_dir():
                continue
            field = field_dir.name
            if filter_field is not None and field != filter_field:
                continue
            for word_dir in sorted(field_dir.iterdir()):
                if not word_dir.is_dir():
                    continue
                word = word_dir.name
                for json_file in word_dir.glob("*.json"):
                    if json_file.is_file():
                        records.append({
                            "path": json_file,
                            "field": field,
                            "word": word,
                            "scope": scope,
                            "score": _score_from_path(json_file),
                        })

    records.sort(key=lambda r: (r["field"], r["word"], r["scope"], -r["score"]))
    return records


# ---------------------------------------------------------------------------
# SAM model loading
# ---------------------------------------------------------------------------

def _infer_sam_config(model_name: str) -> str:
    """Infer the SAM2 Hydra config string from the model filename.

    Falls back to the SAM2.1 base-plus config when the name is ambiguous.
    """
    name = Path(model_name).stem.lower()
    # SAM 2.1 family
    if "2.1" in name or "2_1" in name:
        if "_l" in name:
            return "configs/sam2.1/sam2.1_hiera_l.yaml"
        if "_b+" in name or "_b_plus" in name:
            return "configs/sam2.1/sam2.1_hiera_b+.yaml"
        if "_s" in name:
            return "configs/sam2.1/sam2.1_hiera_s.yaml"
        if "_t" in name:
            return "configs/sam2.1/sam2.1_hiera_t.yaml"
        # e.g. "sam2.1_b" without an explicit size suffix → b+
        return "configs/sam2.1/sam2.1_hiera_b+.yaml"
    # SAM 2.0 family
    if "_l" in name:
        return "configs/sam2/sam2_hiera_l.yaml"
    if "_b+" in name or "_b_plus" in name:
        return "configs/sam2/sam2_hiera_b+.yaml"
    if "_s" in name:
        return "configs/sam2/sam2_hiera_s.yaml"
    if "_t" in name:
        return "configs/sam2/sam2_hiera_t.yaml"
    # Fallback
    return "configs/sam2.1/sam2.1_hiera_b+.yaml"


def load_sam_model(project_path: str, model_name: str):
    """Load a SAM2 automatic mask generator from the project's model directory.

    Returns ``(mask_generator, effective_model_name, device)``.

    Raises
    ------
    ImportError
        When the ``sam2`` package is not installed.
    FileNotFoundError
        When the model checkpoint is not found under ``<project>/models/``.
    RuntimeError
        When the model fails to load.
    """
    try:
        import torch
    except ImportError as exc:
        raise ImportError(
            "PyTorch is required for silhouette generation.\n"
            "Install it with:  pip install torch"
        ) from exc

    try:
        from sam2.build_sam import build_sam2
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
    except ImportError as exc:
        raise ImportError(
            "sam2 is required for silhouette generation.\n"
            "Install it with:  pip install sam2\n"
            "Or from source:   pip install git+https://github.com/facebookresearch/sam2.git"
        ) from exc

    device = "cuda" if torch.cuda.is_available() else "cpu"

    checkpoint = Path(project_path) / "models" / model_name
    if not checkpoint.exists():
        raise FileNotFoundError(
            f"SAM model checkpoint not found: {checkpoint}\n"
            f"Download with: crossing tool model download <hf-repo>"
        )

    config = _infer_sam_config(model_name)

    try:
        sam2_model = build_sam2(config, str(checkpoint), device=device)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to build SAM2 model from '{checkpoint}' "
            f"with config '{config}': {exc}"
        ) from exc

    mask_generator = SAM2AutomaticMaskGenerator(
        model=sam2_model,
        points_per_side=16,
        pred_iou_thresh=0.80,
        stability_score_thresh=0.85,
        min_mask_region_area=500,
    )
    return mask_generator, model_name, device


# ---------------------------------------------------------------------------
# Mask helpers
# ---------------------------------------------------------------------------

def check_containment(mask, border_px: int = _BORDER_CHECK_PX) -> bool:
    """Return True when the mask does not touch any frame edge.

    Rejects a mask if any True pixel falls within *border_px* of the top,
    bottom, left, or right border.

    Parameters
    ----------
    mask:       2-D boolean or 0/1 integer numpy array (H, W).
    border_px:  Width of the border strip in pixels.
    """
    import numpy as np

    m = np.asarray(mask, dtype=bool)
    if m.ndim != 2:
        return False
    h, w = m.shape
    if h < border_px * 2 + 1 or w < border_px * 2 + 1:
        return False

    return not (
        m[:border_px, :].any()
        or m[-border_px:, :].any()
        or m[:, :border_px].any()
        or m[:, -border_px:].any()
    )


def mask_to_polygon(mask) -> list[list[int]] | None:
    """Convert a binary mask to a list of ``[x, y]`` polygon points.

    Uses the Douglas-Peucker simplification via OpenCV.  The largest contour
    by area is selected when multiple disjoint regions are present.

    Returns ``None`` when:
    - No contours are found.
    - The largest contour area is below 100 pixels.
    - The simplified polygon has fewer than ``_MIN_POLYGON_POINTS`` vertices.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ImportError(
            "opencv-python is required for polygon conversion.\n"
            "Install it with:  pip install opencv-python-headless"
        ) from exc

    mask_u8 = (np.asarray(mask, dtype=bool).astype(np.uint8)) * 255
    contours, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        return None

    main_contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(main_contour) < 100:
        return None

    perimeter = cv2.arcLength(main_contour, True)
    epsilon = 0.005 * perimeter  # 0.5% of perimeter — stable but not over-simplified
    approx = cv2.approxPolyDP(main_contour, epsilon, True)

    if len(approx) < _MIN_POLYGON_POINTS:
        return None

    squeezed = approx.squeeze(axis=1)
    if squeezed.ndim != 2 or squeezed.shape[1] != 2:
        return None

    return squeezed.tolist()


def polygon_is_degenerate(points: list) -> bool:
    """Return True when *points* is too small to be a useful polygon."""
    if not points or len(points) < _MIN_POLYGON_POINTS:
        return True
    import numpy as np
    pts = np.array(points, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 2:
        return True
    # Degenerate if bounding box is smaller than 10×10 pixels
    bbox_w = float(pts[:, 0].max() - pts[:, 0].min())
    bbox_h = float(pts[:, 1].max() - pts[:, 1].min())
    return bbox_w < 10 or bbox_h < 10


# ---------------------------------------------------------------------------
# Mask ranking via CLIP
# ---------------------------------------------------------------------------

def rank_masks_by_clip(
    masks: list[dict],
    image_pil,
    word: str,
    clip_model,
    clip_processor,
    device: str,
    min_area_fraction: float = _MIN_MASK_AREA_FRACTION,
    max_area_fraction: float = _MAX_MASK_AREA_FRACTION,
) -> list[tuple[dict, float]]:
    """Score SAM mask crops against *word* using CLIP.

    Only masks whose area fraction falls within [min_area_fraction,
    max_area_fraction] relative to the full frame are considered.

    Returns a list of ``(mask_dict, clip_score)`` tuples sorted by score
    descending.  The ``mask_dict`` is the original SAM output dict (with
    ``"segmentation"``, ``"bbox"``, ``"area"``, …).
    """
    from services.frame_match import clip_score_batch

    w_px, h_px = image_pil.size
    total = w_px * h_px

    valid: list[dict] = []
    for m in masks:
        area = m.get("area", 0)
        if area < total * min_area_fraction:
            continue
        if area > total * max_area_fraction:
            continue
        # Reject extremely elongated masks (landscape ridges, horizon lines, wires)
        bbox = m.get("bbox", [])
        if len(bbox) >= 4:
            bw, bh = float(bbox[2]), float(bbox[3])
            if bw > 0 and bh > 0 and max(bw, bh) / min(bw, bh) > _MAX_ASPECT_RATIO:
                continue
        valid.append(m)

    if not valid:
        return []

    crops = []
    for m in valid:
        x, y, bw, bh = (int(v) for v in m["bbox"])
        # Add 10% padding on each side
        pad_x = max(1, int(bw * 0.10))
        pad_y = max(1, int(bh * 0.10))
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(w_px, x + bw + pad_x)
        y2 = min(h_px, y + bh + pad_y)
        crops.append(image_pil.crop((x1, y1, x2, y2)))

    scores = clip_score_batch(crops, word, clip_model, clip_processor, device)
    paired = list(zip(valid, scores))
    paired.sort(key=lambda t: t[1], reverse=True)
    return paired


# ---------------------------------------------------------------------------
# Tight-crop masked re-scoring (Stage 2 verification)
# ---------------------------------------------------------------------------

def _make_masked_crop(
    image_pil,
    mask_dict: dict,
    pad_fraction: float = _TIGHT_CROP_PAD_FRACTION,
):
    """Return a tight crop of *image_pil* with only the masked region visible.

    Pixels outside the SAM segmentation are replaced with neutral grey
    (128, 128, 128) before cropping to the mask bounding box.  This removes
    surrounding context so that CLIP evaluates the shape and texture of the
    candidate region in isolation.

    Parameters
    ----------
    image_pil:    Full-frame PIL Image (RGB).
    mask_dict:    SAM mask dict with ``"segmentation"`` and ``"bbox"`` fields.
    pad_fraction: Padding fraction around the bbox (default 2%).
    """
    import numpy as np
    from PIL import Image

    w_px, h_px = image_pil.size
    seg = np.asarray(mask_dict["segmentation"], dtype=bool)

    img_arr = np.array(image_pil, dtype=np.uint8)
    neutral = np.full_like(img_arr, 128)
    composite = np.where(seg[:, :, np.newaxis], img_arr, neutral)
    composite_pil = Image.fromarray(composite.astype(np.uint8))

    x, y, bw, bh = (int(v) for v in mask_dict["bbox"])
    pad_x = max(1, int(bw * pad_fraction))
    pad_y = max(1, int(bh * pad_fraction))
    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(w_px, x + bw + pad_x)
    y2 = min(h_px, y + bh + pad_y)
    return composite_pil.crop((x1, y1, x2, y2))


def rerank_by_tight_crop(
    candidates: list[tuple[dict, float]],
    image_pil,
    word: str,
    clip_model,
    clip_processor,
    device: str,
) -> list[tuple[dict, float, float]]:
    """Re-score *candidates* using tight masked crops and re-rank by tight score.

    For each ``(mask_dict, broad_score)`` pair, composites the SAM mask over a
    neutral grey background and crops tightly to the bounding box, then scores
    with CLIP.  This second-stage verification is more discriminative than the
    broad padded-crop pass because surrounding context is removed.

    Returns a list of ``(mask_dict, broad_score, tight_score)`` sorted by
    *tight_score* descending.
    """
    from services.frame_match import clip_score_batch

    if not candidates:
        return []

    crops = [_make_masked_crop(image_pil, m) for m, _ in candidates]
    tight_scores = clip_score_batch(crops, word, clip_model, clip_processor, device)

    result = [
        (m, broad, tight)
        for (m, broad), tight in zip(candidates, tight_scores)
    ]
    result.sort(key=lambda t: t[2], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Candidate search
# ---------------------------------------------------------------------------

def find_candidates(
    project_path: str,
    word: str,
    field: str,
    scope_type: str,
    scope_value: str | None,
    media_type: str = "movies",
    limit: int = 20,
    limit_per_item: int = 3,
) -> list[dict]:
    """Return candidate shots containing *word* in *field*, ranked by score.

    Parameters
    ----------
    scope_type:
        ``"all"``   — full corpus.
        ``"movie"`` — single movie identified by *scope_value* (title or media_id).
        ``"shot"``  — single shot identified by *scope_value* (shot_id string).
    scope_value:
        Movie title / media_id string, or a shot_id.  Ignored when
        ``scope_type == "all"``.
    """
    from services.search import search_shots

    if scope_type == "shot":
        # Single-shot mode: return a minimal placeholder so the caller can
        # process the shot directly without going through the search index.
        return [{"shot_id": scope_value, "score": 1.0, "_single_shot_mode": True}]

    use_all = scope_type == "all"
    scopes = [scope_value] if (scope_type == "movie" and scope_value) else None

    result = search_shots(
        query=word,
        scopes=scopes,
        field=field,
        limit=limit,
        limit_per_item=limit_per_item if not use_all else limit_per_item,
        use_all=use_all,
        project_path=project_path,
        media_type=media_type,
    )
    return result.get("results", [])


# ---------------------------------------------------------------------------
# Frame extraction helper
# ---------------------------------------------------------------------------

def extract_frame_as_pil(video_path: str, frame_index: int, fps: float):
    """Extract *frame_index* from *video_path* and return a PIL Image.

    Returns None when extraction fails.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow is required for frame extraction.\n"
            "Install it with:  pip install Pillow"
        ) from exc

    position = frame_index / fps if fps > 0 else 0.0

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "frame.jpg"
        from services.frame_match import _extract_frame_at
        ok = _extract_frame_at(str(video_path), position, out)
        if not ok:
            return None
        try:
            img = Image.open(str(out)).convert("RGB")
            img.load()
            return img
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Per-shot silhouette extraction
# ---------------------------------------------------------------------------

def _build_rejection(reason: str) -> dict:
    return {"accepted": False, "reason": reason, "payload": None}


def process_shot_silhouette(
    project_path: str,
    word: str,
    field: str,
    shot_id: str,
    filename: str,
    media_id: str,
    media_type: str,
    sam_model_name: str,
    frame_model_name: str,
    verbose: bool = False,
    *,
    clip_model=None,
    clip_processor=None,
    clip_device=None,
    mask_generator=None,
) -> dict:
    """Run the full CLIP + SAM pipeline for a single shot.

    Returns a dict with:
      - ``accepted`` (bool)
      - ``reason``   (str, rejection reason or "ok")
      - ``payload``  (dict or None) — the data to be written to the JSON cache

    The function never writes to disk.  The caller is responsible for saving.
    """
    import numpy as np

    from services.frame_match import (
        _get_video_fps,
        _load_clip_model,
        find_query_best_frame_for_shot,
    )

    video_path = Path(project_path) / "media" / "videos" / media_type / filename
    if not video_path.exists():
        return _build_rejection(f"video file not found: {video_path}")

    # --- parse shot boundaries ---
    try:
        _, start_frame, end_frame = parse_shot_id(shot_id)
    except ValueError as exc:
        return _build_rejection(f"invalid shot_id: {exc}")

    fps = _get_video_fps(str(video_path))

    # --- load CLIP model (if not pre-loaded) ---
    if clip_model is None:
        if verbose:
            print(f"  Loading CLIP model '{frame_model_name}'…")
        try:
            clip_model, clip_processor, clip_device = _load_clip_model(
                project_path, frame_model_name
            )
        except (ImportError, RuntimeError) as exc:
            return _build_rejection(f"CLIP model load failed: {exc}")

    # --- find best frame for *word* in this shot ---
    if verbose:
        print(f"  Finding best frame for '{word}' in {shot_id}…")
    try:
        frame_index, clip_score = find_query_best_frame_for_shot(
            project_path=project_path,
            filename=filename,
            shot_id=shot_id,
            query=word,
            media_type=media_type,
            model=clip_model,
            processor=clip_processor,
            device=clip_device,
        )
    except Exception as exc:
        return _build_rejection(f"frame search failed: {exc}")

    if verbose:
        print(f"  Best frame: {frame_index}  CLIP score: {clip_score:.4f}")

    # --- extract frame image ---
    image_pil = extract_frame_as_pil(str(video_path), frame_index, fps)
    if image_pil is None:
        return _build_rejection(f"frame extraction failed at frame {frame_index}")

    frame_w, frame_h = image_pil.size

    # --- load SAM model (if not pre-loaded) ---
    effective_sam_name = sam_model_name
    if mask_generator is None:
        if verbose:
            print(f"  Loading SAM model '{sam_model_name}'…")
        try:
            mask_generator, effective_sam_name, _ = load_sam_model(
                project_path, sam_model_name
            )
        except (ImportError, FileNotFoundError, RuntimeError) as exc:
            return _build_rejection(f"SAM model load failed: {exc}")

    # --- run SAM ---
    if verbose:
        print(f"  Running SAM segmentation…")
    try:
        masks = mask_generator.generate(np.array(image_pil))
    except Exception as exc:
        return _build_rejection(f"SAM segmentation failed: {exc}")

    if not masks:
        return _build_rejection("SAM produced no masks for this frame")

    if verbose:
        print(f"  SAM produced {len(masks)} mask(s). Running broad CLIP ranking…")

    # --- Stage 1: broad CLIP ranking (padded bbox crops) ---
    ranked = rank_masks_by_clip(
        masks,
        image_pil,
        word,
        clip_model,
        clip_processor,
        clip_device,
    )

    if not ranked:
        return _build_rejection(
            "no masks passed the size/shape filter "
            f"(frame {frame_w}×{frame_h}, {len(masks)} total)"
        )

    # Fast floor check: if even the best broad score is below the minimum,
    # there is nothing semantically relevant to re-score.
    top_broad_score = ranked[0][1]
    if top_broad_score < _CLIP_SCORE_FLOOR:
        if verbose:
            print(f"  ✗ Best broad score {top_broad_score:.4f} < floor {_CLIP_SCORE_FLOOR}")
        return _build_rejection(
            f"best mask CLIP score too low ({top_broad_score:.3f} < floor {_CLIP_SCORE_FLOOR}): "
            f"likely no '{word}' visible in this frame"
        )

    if verbose:
        n = min(len(ranked), _RERANK_TOP_N)
        top_strs = ", ".join(f"{s:.3f}" for _, s in ranked[:n])
        print(f"  Broad top-{n} scores: [{top_strs}]")

    # --- Stage 2: tight-crop re-scoring ---
    # Take the top-N broad candidates, composite each mask over neutral grey,
    # crop tightly to the bbox, and re-score.  This removes surrounding context
    # so CLIP evaluates the shape/texture of the region alone — far more
    # discriminative than the padded crop used in Stage 1.
    top_n_broad = ranked[:_RERANK_TOP_N]
    reranked = rerank_by_tight_crop(
        top_n_broad, image_pil, word, clip_model, clip_processor, clip_device
    )

    if not reranked:
        return _build_rejection(
            "tight-crop re-scoring produced no candidates "
            f"(frame {frame_w}×{frame_h})"
        )

    tight_winner_score = reranked[0][2]

    if verbose:
        tight_strs = ", ".join(f"{s:.3f}" for _, _, s in reranked)
        print(f"  Tight-crop scores: [{tight_strs}]")
        if reranked[0][0] is not ranked[0][0]:
            bb_broad = [int(v) for v in ranked[0][0]["bbox"]]
            bb_tight = [int(v) for v in reranked[0][0]["bbox"]]
            print(f"  ⚠ Tight-crop re-scoring changed the ranking!")
            print(f"    Broad winner bbox: {bb_broad}")
            print(f"    Tight winner bbox: {bb_tight}")

    # Floor check on tight-crop winner
    if tight_winner_score < _CLIP_SCORE_FLOOR:
        if verbose:
            print(
                f"  ✗ Tight-crop winner {tight_winner_score:.4f} < floor {_CLIP_SCORE_FLOOR}"
            )
        return _build_rejection(
            f"tight-crop score too low ({tight_winner_score:.3f} < floor {_CLIP_SCORE_FLOOR}): "
            f"likely no '{word}' visible in this frame"
        )

    # Ambiguity check on tight-crop scores
    if len(reranked) >= 2:
        tight_runner_up = reranked[1][2]
        tight_margin = tight_winner_score - tight_runner_up
        if verbose:
            print(
                f"  Tight-crop margin: {tight_margin:.4f} "
                f"(required ≥ {_CLIP_RUNNER_UP_MARGIN})"
            )
        if tight_margin < _CLIP_RUNNER_UP_MARGIN:
            return _build_rejection(
                f"ambiguous segmentation (tight-crop): "
                f"top={tight_winner_score:.3f}, runner-up={tight_runner_up:.3f}, "
                f"margin={tight_margin:.4f} < required {_CLIP_RUNNER_UP_MARGIN} — "
                f"cannot confidently identify '{word}'"
            )

    # --- Pick best contained mask from tight-ranked order ---
    accepted_mask = None
    accepted_clip_score = 0.0
    accepted_broad_score = 0.0
    rejection_notes: list[str] = []

    for mask_dict, broad_score, tight_score in reranked:
        seg = mask_dict.get("segmentation")
        if seg is None:
            rejection_notes.append("mask has no segmentation")
            continue

        seg_arr = np.asarray(seg, dtype=bool)

        if not check_containment(seg_arr):
            rejection_notes.append(
                f"mask touches border (broad={broad_score:.3f}, tight={tight_score:.3f})"
            )
            continue

        polygon = mask_to_polygon(seg_arr)
        if polygon is None or polygon_is_degenerate(polygon):
            rejection_notes.append(
                f"degenerate polygon (tight={tight_score:.3f})"
            )
            continue

        # Accepted!
        accepted_mask = mask_dict
        accepted_clip_score = tight_score
        accepted_broad_score = broad_score
        accepted_polygon = polygon
        accepted_seg = seg_arr
        break

    if accepted_mask is None:
        reason = "; ".join(rejection_notes[:5]) if rejection_notes else "all masks rejected"
        return _build_rejection(f"no acceptable mask: {reason}")

    # --- compute bbox ---
    bx, by, bw, bh = (int(v) for v in accepted_mask["bbox"])
    bbox = [bx, by, bw, bh]

    # --- build frame provenance path ---
    from services.frame_match import best_frame_path
    bf_path = best_frame_path(project_path, media_type, filename, shot_id)
    frame_provenance = str(bf_path) if bf_path.exists() else f"frame:{frame_index}"

    payload = {
        "word": word,
        "field": field,
        "scope": _scope_label(None, None),  # filled by caller
        "media_type": media_type,
        "source_filename": filename,
        "media_id": media_id,
        "shot_id": shot_id,
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frame_index": frame_index,
        "frame_provenance": frame_provenance,
        "frame_size": [frame_w, frame_h],
        "sam_model": effective_sam_name,
        "frame_match_model": frame_model_name,
        "score": round(accepted_clip_score, 6),
        "score_broad": round(accepted_broad_score, 6),
        "contained": True,
        "rejection_reason": None,
        "bbox": bbox,
        "polygon": accepted_polygon,
        "polygon_simplification": "douglas_peucker_0.005_perimeter",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": SILHOUETTE_VERSION,
    }

    if verbose:
        print(
            f"  ✓ Accepted: tight={accepted_clip_score:.4f}  "
            f"broad={accepted_broad_score:.4f}  "
            f"bbox={bbox}  polygon_pts={len(accepted_polygon)}"
        )

    return {"accepted": True, "reason": "ok", "payload": payload}


def _scope_label(scope_type: str | None, scope_value: str | None) -> str:
    """Return a human-readable scope label for the JSON payload."""
    if scope_type == "all" or scope_type is None:
        return "all"
    if scope_type == "movie":
        return f"movie:{scope_value}" if scope_value else "movie"
    if scope_type == "shot":
        return f"shot:{scope_value}" if scope_value else "shot"
    return scope_type or "unknown"


def _scope_dir_name(scope_type: str, scope_value: str | None, media_id: str | None) -> str:
    """Return the directory component used for scope in the cache path.

    Examples:
      all           → "all"
      movie, tmdb_11969 → "movie-tmdb_11969"
      shot, tmdb_11969@f000812-f000901 → "shot-tmdb_11969@f000812-f000901"
    """
    if scope_type == "all":
        return "all"
    if scope_type == "movie":
        target = media_id or scope_value or "unknown"
        return f"movie-{target}"
    if scope_type == "shot":
        return f"shot-{scope_value or 'unknown'}"
    return scope_type


# ---------------------------------------------------------------------------
# Filename / media_id resolution helpers
# ---------------------------------------------------------------------------

def _resolve_filename_for_media_id(
    project_path: str,
    media_id: str,
    media_type: str,
) -> str | None:
    """Return the video filename for *media_id* or None when not found."""
    for entry in get_metadata(project_path, media_type=media_type):
        if compute_media_id(entry, media_type) == media_id:
            return entry.get("filename")
    return None


def _resolve_shot_details(
    project_path: str,
    shot_id: str,
    media_type: str,
) -> tuple[str | None, str | None]:
    """Return ``(filename, media_id)`` for *shot_id* by scanning annotation files.

    Returns ``(None, None)`` when the shot cannot be located.
    """
    # The media_id is the part before '@' in the shot_id
    try:
        media_id_part, _, _ = parse_shot_id(shot_id)
    except ValueError:
        return None, None

    for entry in get_metadata(project_path, media_type=media_type):
        mid = compute_media_id(entry, media_type)
        if mid == media_id_part:
            return entry.get("filename"), mid

    return None, None


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _manifest_path(project_path: str, media_type: str) -> Path:
    """Return the path to the global silhouette manifest JSON."""
    return Path(project_path) / "data" / "silhouettes" / media_type / "manifest.json"


def load_silhouette_manifest(project_path: str, media_type: str) -> dict:
    """Load (or create) the silhouette manifest for *media_type*."""
    mpath = _manifest_path(project_path, media_type)
    if mpath.exists():
        try:
            return json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"entries": []}


def save_silhouette_manifest(project_path: str, media_type: str, manifest: dict) -> None:
    """Persist the silhouette manifest."""
    mpath = _manifest_path(project_path, media_type)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def _upsert_manifest_entry(manifest: dict, entry: dict) -> None:
    """Add or replace a manifest entry keyed on (word, field, scope, media_type)."""
    key = (entry["word"], entry["field"], entry["scope"], entry["media_type"])
    entries = manifest.setdefault("entries", [])
    for i, e in enumerate(entries):
        if (e["word"], e["field"], e["scope"], e["media_type"]) == key:
            entries[i] = entry
            return
    entries.append(entry)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def build_silhouette(
    project_path: str,
    word: str,
    field: str,
    scope_type: str,
    scope_value: str | None,
    media_type: str = "movies",
    sam_model_name: str = "sam2.1_b.pt",
    frame_model_name: str = "clip-vit-base-patch32",
    force: bool = False,
    verbose: bool = False,
    dry_run: bool = False,
) -> dict:
    """Build and cache the best silhouette polygon for *word* in *field*.

    Parameters
    ----------
    project_path:       Project root directory.
    word:               Vocabulary word (e.g. ``"horse"``).
    field:              Annotation field / category (e.g. ``"animals"``).
    scope_type:         ``"all"``, ``"movie"``, or ``"shot"``.
    scope_value:        Movie title / media_id, or shot_id.  None for "all".
    media_type:         ``"movies"`` or ``"gameplay"``.
    sam_model_name:     SAM2 checkpoint filename inside ``<project>/models/``.
    frame_model_name:   CLIP model name/path for frame matching.
    force:              Overwrite existing cached result.
    verbose:            Print progress information.
    dry_run:            Find candidates but do not run segmentation or write.

    Returns
    -------
    dict with keys:
      - ``accepted``     bool
      - ``reason``       str
      - ``payload``      dict or None
      - ``output_path``  str or None (path written)
      - ``candidates``   list of candidate shot dicts considered
    """
    # ----- scope directory name -----
    # For movie scope, resolve media_id first so the dir name is stable.
    resolved_media_id: str | None = None

    if scope_type == "movie" and scope_value:
        # Try to find matching media_id
        for entry in get_metadata(project_path, media_type=media_type):
            mid = compute_media_id(entry, media_type)
            title = str(entry.get("title") or "")
            stem = Path(entry.get("filename") or "").stem
            q = scope_value.lower().strip()
            if (
                mid == scope_value
                or q == title.lower()
                or q == stem.lower()
                or q in title.lower()
            ):
                resolved_media_id = mid
                break

    scope_dir = _scope_dir_name(scope_type, scope_value, resolved_media_id)

    # ----- check cache -----
    exists, existing_path = silhouette_exists(
        project_path, media_type, scope_dir, field, word
    )
    if exists and not force:
        if verbose:
            print(f"  Cache hit: {existing_path}")
        payload = load_silhouette(existing_path) if existing_path else None
        return {
            "accepted": True,
            "reason": "cached",
            "payload": payload,
            "output_path": str(existing_path) if existing_path else None,
            "candidates": [],
        }

    # ----- find candidates -----
    if verbose:
        print(
            f"  Searching for '{word}' in field='{field}' "
            f"scope={scope_type}:{scope_value or 'all'} media={media_type}…"
        )

    candidates = find_candidates(
        project_path=project_path,
        word=word,
        field=field,
        scope_type=scope_type,
        scope_value=scope_value if scope_type != "all" else None,
        media_type=media_type,
    )

    if not candidates:
        return {
            "accepted": False,
            "reason": f"no shots found for '{word}' in field '{field}'",
            "payload": None,
            "output_path": None,
            "candidates": [],
        }

    if verbose:
        print(f"  Found {len(candidates)} candidate shot(s).")

    if dry_run:
        return {
            "accepted": False,
            "reason": "dry-run",
            "payload": None,
            "output_path": None,
            "candidates": candidates,
        }

    # ----- pre-load models once (fail fast before touching any shots) -----
    from services.frame_match import _load_clip_model

    if verbose:
        print(f"  Loading CLIP model '{frame_model_name}'…")
    try:
        _clip_model, _clip_processor, _clip_device = _load_clip_model(
            project_path, frame_model_name
        )
    except (ImportError, RuntimeError) as exc:
        return {
            "accepted": False,
            "reason": f"CLIP model load failed: {exc}",
            "payload": None,
            "output_path": None,
            "candidates": candidates,
        }

    if verbose:
        print(f"  Loading SAM model '{sam_model_name}'…")
    try:
        _mask_generator, _, _ = load_sam_model(project_path, sam_model_name)
    except (ImportError, FileNotFoundError, RuntimeError) as exc:
        return {
            "accepted": False,
            "reason": f"SAM model load failed: {exc}",
            "payload": None,
            "output_path": None,
            "candidates": candidates,
        }

    # ----- process candidates (best-first) -----
    last_rejection = "no candidates processed"

    for candidate in candidates:
        candidate_shot_id = candidate.get("shot_id")
        if not candidate_shot_id:
            continue

        # Resolve filename / media_id for this candidate
        if candidate.get("_single_shot_mode"):
            # Single-shot mode: caller provided the shot_id directly
            filename, cand_media_id = _resolve_shot_details(
                project_path, candidate_shot_id, media_type
            )
        else:
            candidate_filename = candidate.get("filename")
            if not candidate_filename:
                continue
            filename = candidate_filename
            # Derive media_id from metadata
            cand_media_id = None
            for entry in get_metadata(project_path, media_type=media_type):
                if entry.get("filename") == filename:
                    cand_media_id = compute_media_id(entry, media_type)
                    break

        if not filename or not cand_media_id:
            last_rejection = f"could not resolve filename/media_id for {candidate_shot_id}"
            continue

        if verbose:
            print(
                f"  Processing shot {candidate_shot_id} "
                f"(score={candidate.get('score', '?')}, "
                f"file={filename})…"
            )

        result = process_shot_silhouette(
            project_path=project_path,
            word=word,
            field=field,
            shot_id=candidate_shot_id,
            filename=filename,
            media_id=cand_media_id,
            media_type=media_type,
            sam_model_name=sam_model_name,
            frame_model_name=frame_model_name,
            verbose=verbose,
            clip_model=_clip_model,
            clip_processor=_clip_processor,
            clip_device=_clip_device,
            mask_generator=_mask_generator,
        )

        if not result["accepted"]:
            last_rejection = result["reason"]
            if verbose:
                print(f"  Rejected: {last_rejection}")
            continue

        # ----- accepted: fill in scope + save -----
        payload = result["payload"]
        payload["scope"] = _scope_label(scope_type, scope_value)

        frame_index = payload["frame_index"]
        clip_score = payload["score"]

        out_path = silhouette_json_path(
            project_path=project_path,
            media_type=media_type,
            scope=scope_dir,
            field=field,
            word=word,
            media_id=cand_media_id,
            shot_id=candidate_shot_id,
            frame_index=frame_index,
            score=clip_score,
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # ----- update manifest -----
        manifest = load_silhouette_manifest(project_path, media_type)
        _upsert_manifest_entry(manifest, {
            "word": word,
            "field": field,
            "scope": payload["scope"],
            "scope_dir": scope_dir,
            "media_type": media_type,
            "media_id": cand_media_id,
            "shot_id": candidate_shot_id,
            "frame_index": frame_index,
            "score": clip_score,
            "output_path": str(out_path),
            "timestamp": payload["timestamp"],
        })
        save_silhouette_manifest(project_path, media_type, manifest)

        if verbose:
            print(f"  Saved: {out_path}")

        return {
            "accepted": True,
            "reason": "ok",
            "payload": payload,
            "output_path": str(out_path),
            "candidates": candidates,
        }

    # No candidate succeeded
    return {
        "accepted": False,
        "reason": f"all candidates rejected — last: {last_rejection}",
        "payload": None,
        "output_path": None,
        "candidates": candidates,
    }
