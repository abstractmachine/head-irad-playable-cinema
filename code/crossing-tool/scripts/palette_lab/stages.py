"""Pipeline stages for the palette research laboratory.

Each stage is a plain function that takes explicit inputs and returns a plain
dict.  Nothing here writes files — ``run_experiment.py`` owns the output
directory and ``render.py`` owns the images.  Production components are
imported, never copied, with one documented exception (``seeds_labels``).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

# Production constants and algorithms.  Imported at module scope because this
# module is only ever loaded by the experiment runner, never by production.
from data.palette import (
    _FIG_N_CLUSTERS,
    _FIG_RESIZE,
    _SEEDS_DOUBLE_STEP,
    _SEEDS_HISTOGRAM_BINS,
    _SEEDS_ITERATIONS,
    _SEEDS_NUM_LEVELS,
    _SEEDS_PRIOR,
    _SEEDS_TARGET_SUPERPIXELS,
    _agglomerative_palette,
    _detect_content_bbox,
    _rgb_to_lab,
)


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

def resolve_source(project_path: str, media: str, media_type: str) -> dict:
    """Resolve a media identifier or filename fragment to an image on disk."""
    from data.media_id import compute_media_id
    from data.metadata import get_metadata, resolve_thumbnail_path

    entries = get_metadata(project_path, media_type=media_type)
    needle = media.strip().casefold()
    match = None
    for entry in entries:
        media_id = str(entry.get("media_id") or compute_media_id(entry, media_type))
        filename = str(entry.get("filename", ""))
        if needle in (media_id.casefold(), filename.casefold()) or needle in filename.casefold():
            match = entry
            break
    if match is None:
        raise LookupError(
            f"No {media_type} metadata entry matches {media!r}. "
            f"Known: {[e.get('filename') for e in entries]}"
        )

    filename = str(match.get("filename", ""))
    media_id = str(match.get("media_id") or compute_media_id(match, media_type))
    source_path = resolve_thumbnail_path(project_path, media_type, filename)
    if source_path is None:
        raise FileNotFoundError(
            f"No thumbnail image resolved for {filename!r} ({media_type})"
        )

    return {
        "media_id": media_id,
        "filename": filename,
        "media_type": media_type,
        "source_path": Path(source_path),
        "title": str(match.get("title", "")),
        "year": str(match.get("year", "")),
        "director": str(match.get("director", "")),
    }


def resolve_annotation(project_path: str, source: dict) -> dict | None:
    """Return the production shot annotation for this image, or None.

    Mode A (annotation-assisted) uses the result; Mode B (context-assisted)
    runs with None.  Failure is not an error — it is the Mode B condition.
    """
    from data.metadata import get_metadata
    from data.palette import _resolve_thumbnail_annotation

    try:
        entries = get_metadata(project_path, media_type=source["media_type"])
        metadata = next(
            (e for e in entries if e.get("filename") == source["filename"]), {}
        )
        annotation = _resolve_thumbnail_annotation(
            project_path,
            source["filename"],
            source["media_type"],
            source["media_id"],
            metadata,
        )
        return annotation or None
    except Exception:
        return None


def prepare_images(source_path: Path) -> dict:
    """Letterbox-crop and resize exactly as the production pipeline does."""
    from PIL import Image

    with Image.open(source_path) as opened:
        source = opened.convert("RGB")
    content = _detect_content_bbox(source)
    active = source.crop(tuple(content["bbox"]))
    working = active.resize((_FIG_RESIZE, _FIG_RESIZE), Image.LANCZOS)
    return {
        "source": source,
        "active": active,
        "working": working,
        "working_rgb": np.asarray(working),
        "content": content,
    }


# ---------------------------------------------------------------------------
# Stage 1 — Qwen interpreter
# ---------------------------------------------------------------------------

def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value if str(item).strip())
    return str(value)


def focus_variables(source: dict, annotation: dict | None, kind: str) -> dict:
    """Build the ``$name`` substitution map for the focus user prompt."""
    annotation = annotation or {}
    return {
        "kind": kind,
        "title": source.get("title") or source.get("filename", ""),
        "year": source.get("year", ""),
        "director": source.get("director", ""),
        "described": _as_text(annotation.get("description")),
        "setting": _as_text(annotation.get("setting")),
        "framing": _as_text(annotation.get("shot")),
        "timeofday": _as_text(annotation.get("time_of_day")),
        "humans": _as_text(annotation.get("humans")),
        "animals": _as_text(annotation.get("animals")),
        "objects": _as_text(annotation.get("objects")),
        "wearing": _as_text(annotation.get("wearing")),
        "action": _as_text(annotation.get("action")),
    }


def call_qwen(pipeline, system_text: str, user_text: str, images: list, max_new_tokens: int) -> dict:
    """One vision-language call; always preserves the raw text."""
    from data.annotate import _call_model, _extract_json

    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]
    full, generated, device_log = _call_model(
        pipeline,
        messages,
        overrides={"max_new_tokens": max_new_tokens, "do_sample": False},
        images=images,
    )
    return {
        "raw": generated,
        "parsed": _extract_json(generated),
        "device": device_log,
        "image_count": len(images),
    }


# ---------------------------------------------------------------------------
# Stage 2 — SAM3 spatialization
# ---------------------------------------------------------------------------

def _phrase_ladder(block: dict | None) -> list[str]:
    """Ordered segmentation attempts: the phrase, then its fallbacks."""
    block = block or {}
    ladder: list[str] = []
    for value in [block.get("segment_phrase")] + list(block.get("segment_fallbacks") or []):
        phrase = str(value or "").strip()
        if phrase and phrase.casefold() not in {p.casefold() for p in ladder}:
            ladder.append(phrase)
    return ladder


def _downscale_mask(mask: np.ndarray, size: int) -> np.ndarray:
    from PIL import Image

    if mask.shape == (size, size):
        return mask
    image = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
    return np.asarray(image.resize((size, size), Image.Resampling.NEAREST)) > 127


def spatialize(segmenter, images: dict, ladder: list[str], *, resolution: str) -> dict:
    """Walk the phrase ladder until SAM3 returns usable masks.

    *resolution* is ``working`` (256x256, production-identical) or ``active``
    (full-resolution cropped image, masks downsampled afterwards).  Every
    attempt is recorded, including the ones that found nothing.
    """
    target = images["working"] if resolution == "working" else images["active"]
    attempts: list[dict] = []
    accepted_phrase = None
    accepted_masks: list[dict] = []

    for phrase in ladder:
        record = {"phrase": phrase, "mask_count": 0, "error": None, "masks": []}
        try:
            masks = segmenter.segment_concept(target, phrase)
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            attempts.append(record)
            continue

        usable = []
        for index, mask in enumerate(masks):
            segmentation = np.asarray(mask.get("segmentation"), dtype=bool)
            if not segmentation.any():
                continue
            scaled = _downscale_mask(segmentation, _FIG_RESIZE)
            if not scaled.any():
                continue
            rows, cols = np.nonzero(scaled)
            usable.append({
                "index": index,
                "mask": scaled,
                "area": int(scaled.sum()),
                "coverage": round(float(scaled.mean()), 4),
                "centroid": [
                    round(float(cols.mean()) / _FIG_RESIZE, 4),
                    round(float(rows.mean()) / _FIG_RESIZE, 4),
                ],
                "bbox": [int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())],
                "score": round(float(mask.get("predicted_iou", 0.0)), 4),
            })

        record["mask_count"] = len(usable)
        record["masks"] = [
            {k: v for k, v in item.items() if k != "mask"} for item in usable
        ]
        attempts.append(record)

        if usable:
            accepted_phrase = phrase
            accepted_masks = usable
            break

    union = np.zeros((_FIG_RESIZE, _FIG_RESIZE), dtype=bool)
    for item in accepted_masks:
        union |= item["mask"]

    return {
        "requested": ladder[0] if ladder else None,
        "accepted_phrase": accepted_phrase,
        "ladder_rung": ladder.index(accepted_phrase) if accepted_phrase else None,
        "attempts": attempts,
        "masks": accepted_masks,
        "union": union,
        "resolution": resolution,
    }


# ---------------------------------------------------------------------------
# Stage 3 — SEEDS spatial decomposition
# ---------------------------------------------------------------------------

def seeds_labels(arr_rgb: np.ndarray) -> np.ndarray:
    """Run OpenCV SEEDS with the production constants.

    ``data.palette._extract_seeds_superpixels`` cannot be reused directly
    because it requires exactly two disjoint covering masks and raises when
    either is empty; this experiment needs three-way (focus / ambience /
    residual) ownership and must survive an empty region.  The SEEDS
    parameters are imported rather than restated so they cannot drift.
    """
    import cv2

    height, width = arr_rgb.shape[:2]
    seeds = cv2.ximgproc.createSuperpixelSEEDS(
        width,
        height,
        3,
        _SEEDS_TARGET_SUPERPIXELS,
        _SEEDS_NUM_LEVELS,
        _SEEDS_PRIOR,
        _SEEDS_HISTOGRAM_BINS,
        _SEEDS_DOUBLE_STEP,
    )
    seeds.iterate(np.ascontiguousarray(arr_rgb[:, :, ::-1]), _SEEDS_ITERATIONS)
    return np.asarray(seeds.getLabels())


def decompose(arr_rgb: np.ndarray, focus_mask: np.ndarray, ambience_mask: np.ndarray) -> dict:
    """Assign every superpixel a fractional membership in each region.

    Ownership is the largest fraction, ties resolved focus > ambience >
    residual.  The fractions are retained so a later experiment can weight
    Ward by partial membership without re-running anything.
    """
    labels = seeds_labels(arr_rgb)
    exclusive_ambience = ambience_mask & ~focus_mask
    residual_mask = ~(focus_mask | exclusive_ambience)

    height, width = labels.shape
    diagonal = math.hypot(width, height)
    records: list[dict] = []
    boundary_count = 0

    for label_id in np.unique(labels):
        label_mask = labels == label_id
        area = int(label_mask.sum())
        counts = {
            "focus": int(np.count_nonzero(label_mask & focus_mask)),
            "ambience": int(np.count_nonzero(label_mask & exclusive_ambience)),
            "residual": int(np.count_nonzero(label_mask & residual_mask)),
        }
        occupied = [name for name, value in counts.items() if value]
        if len(occupied) > 1:
            boundary_count += 1
        owner = max(("focus", "ambience", "residual"), key=lambda name: counts[name])

        owned = label_mask & {
            "focus": focus_mask,
            "ambience": exclusive_ambience,
            "residual": residual_mask,
        }[owner]
        rows, cols = np.nonzero(label_mask)
        mean_rgb = np.rint(arr_rgb[owned].astype(np.float64).mean(axis=0)).astype(np.uint8)
        lab = _rgb_to_lab(mean_rgb.reshape(1, 3))[0]

        records.append({
            "label": int(label_id),
            "pixel_count": area,
            "owner": owner,
            "region_pixel_count": int(owned.sum()),
            "fractions": {
                name: round(value / area, 4) for name, value in counts.items()
            },
            "boundary": len(occupied) > 1,
            "rgb": mean_rgb.tolist(),
            "lab": [round(float(v), 1) for v in lab],
            "centroid": [
                round(float(cols.mean()) / width, 4),
                round(float(rows.mean()) / height, 4),
            ],
            "bbox": [int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())],
        })

    focus_centroid = _region_centroid(focus_mask)
    for record in records:
        if focus_centroid is None:
            record["focus_distance"] = None
            continue
        dx = record["centroid"][0] * width - focus_centroid[0]
        dy = record["centroid"][1] * height - focus_centroid[1]
        record["focus_distance"] = round(math.hypot(dx, dy) / diagonal, 4)

    return {
        "labels": labels,
        "superpixels": records,
        "summary": {
            "superpixel_count": len(records),
            "focus_superpixels": sum(1 for r in records if r["owner"] == "focus"),
            "ambience_superpixels": sum(1 for r in records if r["owner"] == "ambience"),
            "residual_superpixels": sum(1 for r in records if r["owner"] == "residual"),
            "boundary_superpixels": boundary_count,
            "focus_pixel_coverage": round(float(focus_mask.mean()), 4),
            "ambience_pixel_coverage": round(float(exclusive_ambience.mean()), 4),
            "residual_pixel_coverage": round(float(residual_mask.mean()), 4),
            "focus_ambience_overlap": round(
                float((focus_mask & ambience_mask).mean()), 4
            ),
            "ownership_rule": "largest_fraction_focus_wins_ties",
        },
        "masks": {
            "focus": focus_mask,
            "ambience": exclusive_ambience,
            "residual": residual_mask,
        },
    }


def _region_centroid(mask: np.ndarray):
    if not mask.any():
        return None
    rows, cols = np.nonzero(mask)
    return float(cols.mean()), float(rows.mean())


# ---------------------------------------------------------------------------
# Stage 4 — LAB / Ward candidate measurement
# ---------------------------------------------------------------------------

def measure_candidates(
    arr_rgb: np.ndarray,
    decomposition: dict,
    region: str,
    *,
    n_candidates: int,
) -> dict:
    """Run the production Ward clustering over one region's superpixels.

    Returns candidates ranked exactly as production ranks them, each mapped
    back to the pixels that produced it.  Ward is *candidate generation* here:
    nothing in this function picks a winner.
    """
    labels = decomposition["labels"]
    members = [r for r in decomposition["superpixels"] if r["owner"] == region]
    if len(members) < 2:
        return {
            "region": region,
            "error": f"region '{region}' has {len(members)} superpixels; Ward needs 2",
            "candidates": [],
        }

    payload = [
        {"label": r["label"], "rgb": r["rgb"], "pixel_count": r["pixel_count"]}
        for r in members
    ]
    capture: dict = {}
    try:
        ranked = _agglomerative_palette(
            payload,
            _FIG_N_CLUSTERS,
            n_candidates,
            diagnostic_capture=capture,
        )
    except Exception as exc:
        return {
            "region": region,
            "error": f"{type(exc).__name__}: {exc}",
            "candidates": [],
        }

    membership: dict[int, list[int]] = {}
    for label, cluster_id in zip(capture["superpixel_labels"], capture["cluster_labels"]):
        membership.setdefault(int(cluster_id), []).append(int(label))

    region_mask = decomposition["masks"][region]
    region_pixels = int(region_mask.sum())
    focus_centroid = _region_centroid(decomposition["masks"]["focus"])
    height, width = labels.shape
    diagonal = math.hypot(width, height)

    candidates: list[dict] = []
    for index, (candidate, cluster) in enumerate(zip(ranked, capture["clusters"])):
        member_labels = membership.get(int(cluster["cluster_id"]), [])
        pixel_mask = np.isin(labels, member_labels) & region_mask
        rows, cols = np.nonzero(pixel_mask)
        if len(rows) == 0:
            centre = [None, None]
            spread = None
            focus_distance = None
        else:
            cx, cy = float(cols.mean()), float(rows.mean())
            centre = [round(cx / width, 4), round(cy / height, 4)]
            spread = round(
                float(np.sqrt(((cols - cx) ** 2 + (rows - cy) ** 2).mean())) / diagonal,
                4,
            )
            focus_distance = (
                None
                if focus_centroid is None
                else round(
                    math.hypot(cx - focus_centroid[0], cy - focus_centroid[1]) / diagonal,
                    4,
                )
            )

        lab = candidate["lab"]
        rgb = list(candidate["rgb"])
        candidates.append({
            "index": index,
            "cluster_id": int(cluster["cluster_id"]),
            "rgb": rgb,
            "hex": "#{:02x}{:02x}{:02x}".format(*rgb),
            "lab": lab,
            "lightness": round(float(lab[0]), 1),
            "chroma": round(float(math.hypot(lab[1], lab[2])), 1),
            "hue": round(float(math.degrees(math.atan2(lab[2], lab[1])) % 360), 1),
            "pixel_count": int(pixel_mask.sum()),
            "coverage": round(float(pixel_mask.sum()) / max(1, region_pixels), 4),
            "frame_coverage": round(float(pixel_mask.mean()), 4),
            "superpixel_count": len(member_labels),
            "centre": centre,
            "spread": spread,
            "focus_distance": focus_distance,
            "superpixel_labels": member_labels,
            "production_weight": round(float(candidate["weight"]), 2),
        })

    return {
        "region": region,
        "error": None,
        "region_pixels": region_pixels,
        "superpixel_count": len(members),
        "cluster_count": _FIG_N_CLUSTERS,
        "candidates": candidates,
    }


def candidate_mask(labels: np.ndarray, candidate: dict, region_mask: np.ndarray) -> np.ndarray:
    return np.isin(labels, candidate["superpixel_labels"]) & region_mask


def candidate_table(measurement: dict) -> str:
    """Compact text table of measured candidates for the curator prompt."""
    if measurement.get("error"):
        return f"(no candidates: {measurement['error']})"
    lines = []
    for c in measurement["candidates"]:
        lines.append(
            f"  [{c['index']}] {c['hex']}  rgb={c['rgb']}  "
            f"lab=({c['lab'][0]}, {c['lab'][1]}, {c['lab'][2]})  "
            f"lightness={c['lightness']}  chroma={c['chroma']}  "
            f"coverage={c['coverage']}  regions={c['superpixel_count']}  "
            f"spread={c['spread']}  centre={c['centre']}  "
            f"focus_distance={c['focus_distance']}"
        )
    return "\n".join(lines)
