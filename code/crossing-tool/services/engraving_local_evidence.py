"""Evidence assembly for the local engraving converter.

Stage 1 of the pipeline.  Turns a silhouette catalog record into the exact set
of images handed to the model, plus a recorded coordinate transform and an
honest assessment of how well the evidence determines the object.

Reference order, strongest first (see the engraving review notes):

1. the exact silhouette and its binary mask;
2. the exact extraction frame;
3. at most two auxiliary same-shot context frames.

The silhouette governs identity, visible pose, orientation, proportions and
distinctive damage.  Auxiliary references may only clarify topology,
articulation and construction.  The ``label`` field is fallible retrieval
metadata and never overrides visible evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

# Catalog fields that describe how complete the extracted object is.  These
# drive the review flags rather than any learned uncertainty estimate.
_OCCLUSION_FIELD = "occlusion"
_COMPLETENESS_FIELD = "completeness"
_FRAGMENT_FIELD = "fragment_count"
_TOUCHES_FRAME_FIELD = "touches_frame"


class EvidenceError(RuntimeError):
    """Raised when required evidence cannot be assembled."""


def load_silhouette_record(source_json: str | Path) -> dict:
    """Read and lightly validate a silhouette catalog object JSON."""
    source_json = Path(source_json)
    if not source_json.is_file():
        raise EvidenceError(f"Silhouette record not found: {source_json}")
    try:
        record = json.loads(source_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"Silhouette record is not valid JSON: {source_json}\n  {exc}") from exc
    if not isinstance(record, dict):
        raise EvidenceError(f"Silhouette record must be a JSON object: {source_json}")
    return record


def resolve_silhouette_png(source_json: str | Path, record: dict) -> Path:
    """Return the sibling silhouette PNG for a catalog record."""
    source_json = Path(source_json)
    parent = source_json.parent

    png_name = record.get("png")
    if png_name:
        candidate = parent / png_name
        if candidate.is_file():
            return candidate

    candidate = source_json.with_suffix(".png")
    if candidate.is_file():
        return candidate

    raise EvidenceError(
        f"Could not resolve silhouette PNG for:\n  {source_json}\n"
        f"Tried record['png']={png_name!r} and sibling PNG:\n  {candidate}"
    )


def resolve_source_frame(record: dict) -> Path | None:
    """Return the exact extraction frame path if it exists on disk."""
    raw = record.get("source_frame")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def find_context_frames(record: dict, limit: int) -> list[Path]:
    """Return up to *limit* auxiliary frames from the same shot.

    Only same-shot siblings are considered.  A different shot of the same
    subtype is deliberately excluded here: it is far more likely to donate
    unsupported ornament or a more familiar silhouette than to clarify
    topology.
    """
    if limit <= 0:
        return []

    exact = resolve_source_frame(record)
    if exact is None:
        return []

    shot_dir = exact.parent
    if not shot_dir.is_dir():
        return []

    shot_id = str(record.get("shot_id") or "")
    found: list[Path] = []
    for candidate in sorted(shot_dir.glob("*.png")):
        if candidate == exact:
            continue
        if shot_id and shot_id not in candidate.stem:
            continue
        found.append(candidate)
        if len(found) >= limit:
            break
    return found


def binary_mask(silhouette_rgba):
    """Return the silhouette's binary alpha mask as a PIL ``L`` image."""
    if "A" not in silhouette_rgba.getbands():
        raise EvidenceError("Silhouette PNG has no alpha channel to use as a mask")
    alpha = silhouette_rgba.getchannel("A")
    return alpha.point(lambda value: 255 if value >= 128 else 0)


def _padded_bbox(bbox: tuple[int, int, int, int], size: tuple[int, int], fraction: float):
    """Expand *bbox* by *fraction* of its longest side, clamped to *size*."""
    x0, y0, x1, y1 = bbox
    width, height = size
    pad = int(round(max(x1 - x0, y1 - y0) * fraction))
    return (
        max(0, x0 - pad),
        max(0, y0 - pad),
        min(width, x1 + pad),
        min(height, y1 + pad),
    )


def build_working_images(
    silhouette_png: str | Path,
    *,
    working_size: int,
    background_level: int,
    crop_padding_fraction: float,
) -> dict:
    """Composite the silhouette onto a square near-white canvas.

    Returns a dict with the working images and the single coordinate transform
    shared by every derived raster, so any later stage can map model output
    back onto original silhouette pixels.
    """
    from PIL import Image

    silhouette_png = Path(silhouette_png)
    with Image.open(silhouette_png) as opened:
        rgba = opened.convert("RGBA")

    mask = binary_mask(rgba)
    bbox = mask.getbbox()
    if bbox is None:
        raise EvidenceError(f"Silhouette mask is empty: {silhouette_png}")

    crop_box = _padded_bbox(bbox, rgba.size, crop_padding_fraction)
    cropped = rgba.crop(crop_box)
    cropped_mask = mask.crop(crop_box)

    crop_w, crop_h = cropped.size
    scale = working_size / max(crop_w, crop_h)
    scaled_w = max(1, int(round(crop_w * scale)))
    scaled_h = max(1, int(round(crop_h * scale)))

    resized = cropped.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
    resized_mask = cropped_mask.resize((scaled_w, scaled_h), Image.Resampling.NEAREST)

    offset_x = (working_size - scaled_w) // 2
    offset_y = (working_size - scaled_h) // 2

    level = int(background_level)
    canvas = Image.new("RGB", (working_size, working_size), (level, level, level))
    canvas.paste(resized.convert("RGB"), (offset_x, offset_y), resized_mask)

    mask_canvas = Image.new("L", (working_size, working_size), 0)
    mask_canvas.paste(resized_mask, (offset_x, offset_y))

    return {
        "subject": canvas,
        "mask": mask_canvas,
        "transform": {
            "source_size": [rgba.width, rgba.height],
            "mask_bbox": list(bbox),
            "crop_box": list(crop_box),
            "crop_padding_fraction": float(crop_padding_fraction),
            "scale": float(scale),
            "scaled_size": [scaled_w, scaled_h],
            "paste_offset": [offset_x, offset_y],
            "working_size": [working_size, working_size],
            "background_level": level,
            "resample_subject": "LANCZOS",
            "resample_mask": "NEAREST",
        },
    }


def assess_evidence(record: dict, config: dict) -> dict:
    """Return review flags derived from the catalog record's own quality fields.

    These are archive facts, not model opinions: an object the segmentation
    stage already marked heavily occluded or cropped cannot be completed
    confidently, and the correct output is a review flag rather than a
    confident invention.
    """
    rules = config.get("validate", {})
    reasons: list[str] = []

    occlusion = str(record.get(_OCCLUSION_FIELD) or "").lower()
    if occlusion in {str(v).lower() for v in rules.get("review_on_occlusion", ())}:
        reasons.append(
            f"source object occlusion is '{occlusion}'; "
            f"missing structure is not determined by the evidence"
        )

    completeness = str(record.get(_COMPLETENESS_FIELD) or "").lower()
    if completeness in {str(v).lower() for v in rules.get("review_on_completeness", ())}:
        reasons.append(
            f"source object completeness is '{completeness}'; "
            f"its full extent is not visible in the frame"
        )

    fragments = record.get(_FRAGMENT_FIELD)
    threshold = rules.get("review_on_fragment_count")
    if isinstance(fragments, int) and isinstance(threshold, int) and fragments >= threshold:
        reasons.append(f"mask is split into {fragments} fragments; connectivity is ambiguous")

    if record.get(_TOUCHES_FRAME_FIELD):
        edges = record.get("edge_touch") or []
        where = f" ({', '.join(str(e) for e in edges)})" if edges else ""
        reasons.append(f"mask touches the frame edge{where}; the object continues outside the image")

    return {
        "ambiguous": bool(reasons),
        "reasons": reasons,
        "signals": {
            "occlusion": record.get(_OCCLUSION_FIELD),
            "completeness": record.get(_COMPLETENESS_FIELD),
            "fragment_count": record.get(_FRAGMENT_FIELD),
            "touches_frame": record.get(_TOUCHES_FRAME_FIELD),
            "engraving_score": record.get("engraving_score"),
            "semantic_confidence": record.get("semantic_confidence"),
        },
    }


def assemble_evidence(source_json: str | Path, config: dict) -> dict:
    """Assemble every input the repair stage needs, with full provenance.

    Returns a dict with keys ``record``, ``silhouette_png``, ``source_frame``,
    ``context_frames``, ``subject``, ``mask``, ``transform``, ``review`` and
    ``provenance``.
    """
    source_json = Path(source_json)
    record = load_silhouette_record(source_json)
    silhouette_png = resolve_silhouette_png(source_json, record)

    evidence_cfg = config.get("evidence", {})
    working = build_working_images(
        silhouette_png,
        working_size=evidence_cfg["working_size"],
        background_level=evidence_cfg["background_level"],
        crop_padding_fraction=evidence_cfg["crop_padding_fraction"],
    )

    source_frame = resolve_source_frame(record) if evidence_cfg.get("use_source_frame") else None
    context_frames = find_context_frames(record, int(evidence_cfg.get("max_context_frames", 0)))

    return {
        "record": record,
        "source_json": str(source_json),
        "silhouette_png": str(silhouette_png),
        "source_frame": str(source_frame) if source_frame else None,
        "context_frames": [str(p) for p in context_frames],
        "subject": working["subject"],
        "mask": working["mask"],
        "transform": working["transform"],
        "review": assess_evidence(record, config),
        "provenance": {
            "candidate_id": str(source_json),
            "media_type": record.get("media_type"),
            "filename": record.get("filename"),
            "filename_stem": record.get("filename_stem"),
            "media_id": record.get("media_id"),
            "shot_id": record.get("shot_id"),
            "frame": record.get("frame"),
            "label": record.get("label"),
            "field": record.get("field"),
            "frame_size": record.get("frame_size"),
            "bbox": record.get("bbox"),
            "mask_area": record.get("mask_area"),
            "sam_model": record.get("sam_model"),
        },
    }
