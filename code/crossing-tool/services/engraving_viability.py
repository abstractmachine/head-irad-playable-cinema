"""Visual-viability measurement and gating for engraving candidates.

This answers a different question from reconstruction difficulty:

``visual viability``
    Is there enough visual evidence here to make this a meaningful
    reconstruction problem at all?
``reconstruction difficulty``
    Given adequate evidence, how much is missing, occluded or fragmented?

The bird is the canonical case of *viable but difficult*: heavily occluded and
cropped, yet large and legible enough to be worth reconstructing.  A tiny
blurred fragment is the opposite -- easy to describe, impossible to evaluate.

Metrics are deliberately few and interpretable.  Western source material is
grainy, faded, low-light and optically soft, so the metrics are chosen to
identify *grossly unusable* crops rather than to impose modern digital-camera
sharpness standards.  All of them are computed on the silhouette PNG's own
opaque region, which already exists on disk, so nothing decodes a movie.
"""

from __future__ import annotations

import json
from pathlib import Path

# Bump when a metric's definition changes so cached rows can be invalidated.
METRICS_VERSION = 2


def _luminance(rgb_array):
    import numpy as np

    return (
        0.299 * rgb_array[..., 0].astype(np.float32)
        + 0.587 * rgb_array[..., 1].astype(np.float32)
        + 0.114 * rgb_array[..., 2].astype(np.float32)
    )


def measure_silhouette(png_path: str | Path) -> dict | None:
    """Return interpretable viability metrics for one silhouette PNG.

    Returns ``None`` when the image cannot be read.  Every measurement is
    restricted to the object's opaque pixels so surrounding transparency
    cannot inflate or deflate a score.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    png_path = Path(png_path)
    if not png_path.is_file():
        return None

    try:
        with Image.open(png_path) as opened:
            rgba = opened.convert("RGBA")
    except Exception:
        return None

    array = np.asarray(rgba, dtype=np.uint8)
    if array.size == 0:
        return None

    alpha = array[..., 3]
    mask = alpha > 127
    height, width = mask.shape
    mask_pixels = int(mask.sum())
    if mask_pixels == 0:
        return None

    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    bbox_h = int(rows[-1] - rows[0] + 1)
    bbox_w = int(cols[-1] - cols[0] + 1)

    luminance = _luminance(array)
    # Blank the exterior so gradients at the cut-out border are not counted as
    # object structure.
    inside = luminance.copy()
    inside[~mask] = 0.0

    # Sharpness: variance of the Laplacian over object pixels only.
    laplacian = ndimage.laplace(inside)
    laplacian_variance = float(laplacian[mask].var())

    # Structural information: mean Sobel gradient magnitude over object pixels.
    gx = ndimage.sobel(inside, axis=1)
    gy = ndimage.sobel(inside, axis=0)
    gradient = np.hypot(gx, gy)
    edge_energy = float(gradient[mask].mean())

    # Edge density: fraction of object pixels carrying a gradient above an
    # absolute floor. An earlier version thresholded at the crop's own 75th
    # percentile, which made the metric 0.25 by construction and useless.
    object_gradient = gradient[mask]
    edge_density = float((object_gradient > 32.0).mean()) if object_gradient.size else 0.0

    # Local contrast: luminance spread inside the object.
    contrast = float(luminance[mask].std())

    # Segmentation coherence.
    labels, components = ndimage.label(mask)
    if components > 1:
        sizes = ndimage.sum(mask, labels, range(1, components + 1))
        largest_fraction = float(sizes.max() / mask_pixels)
    else:
        largest_fraction = 1.0

    return {
        "metrics_version": METRICS_VERSION,
        "image_width_px": int(width),
        "image_height_px": int(height),
        "bbox_width_px": bbox_w,
        "bbox_height_px": bbox_h,
        "short_dimension_px": int(min(bbox_w, bbox_h)),
        "long_dimension_px": int(max(bbox_w, bbox_h)),
        "bbox_area_px": int(bbox_w * bbox_h),
        "mask_area_px": mask_pixels,
        "mask_fill_fraction": round(mask_pixels / float(bbox_w * bbox_h), 6),
        "laplacian_variance": round(laplacian_variance, 4),
        "edge_energy": round(edge_energy, 4),
        "edge_density": round(edge_density, 6),
        "local_contrast": round(contrast, 4),
        "connected_components": int(components),
        "largest_component_fraction": round(largest_fraction, 6),
    }


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

# Derived from the 40 human-labelled proposal-1 candidates; see
# `crossing engraving benchmark analyze-viability`.
#
# Measured finding: sharpness-style metrics (Laplacian variance, edge energy,
# local contrast) did **not** separate the human's accept/reject groups at all
# (medians nearly identical, 0.71-0.83 overlap).  Only geometry separated, and
# only weakly.  The gate therefore does exactly what the measurements support:
# it removes objects that are too small or too fragmented to evaluate, and it
# keeps a single very low contrast floor to catch degenerate flat crops.
#
# Floors sit just below the weakest human-accepted anchor, so the gate costs
# zero accepted anchors.  It is a viability floor, not a taste model.
DEFAULT_GATE = {
    "gate_version": 2,
    "metrics_version": METRICS_VERSION,
    # Weakest accepted anchor: bench-022 lamp (short 71, area 6419, frac 0.0041).
    "min_short_dimension_px": 70,
    "min_mask_area_px": 6400,
    "min_mask_area_fraction": 0.004,
    # Weakest accepted anchor: bench-013 chair (0.5041).
    "min_largest_component_fraction": 0.50,
    # Weakest accepted anchor: bench-013 chair (9.78). Only bench-010 rock
    # (4.6) falls below, which is the intended degenerate-crop case.
    "min_local_contrast": 5.0,
}


def evaluate_gate(metrics: dict | None, record: dict | None = None, gate: dict | None = None) -> dict:
    """Return a pass/fail decision with an explicit reason for each check."""
    gate = gate or DEFAULT_GATE
    record = record or {}

    if not metrics:
        return {
            "viable": False,
            "gate_version": gate["gate_version"],
            "reasons": ["metrics_unavailable"],
            "checks": {},
        }
    if metrics.get("metrics_version") != gate["metrics_version"]:
        return {
            "viable": False,
            "gate_version": gate["gate_version"],
            "metrics_version": metrics.get("metrics_version"),
            "reasons": ["metrics_version_mismatch"],
            "checks": {},
        }

    checks: dict[str, bool] = {}
    reasons: list[str] = []

    def _require(name: str, value, minimum, label: str) -> None:
        ok = value is not None and value >= minimum
        checks[name] = bool(ok)
        if not ok:
            reasons.append(f"{label} {value} < {minimum}")

    _require("short_dimension", metrics.get("short_dimension_px"),
             gate["min_short_dimension_px"], "short_dimension_px")
    _require("mask_area", metrics.get("mask_area_px"),
             gate["min_mask_area_px"], "mask_area_px")
    _require("segmentation_coherence", metrics.get("largest_component_fraction"),
             gate["min_largest_component_fraction"], "largest_component_fraction")

    fraction = record.get("mask_area_fraction")
    if fraction is not None:
        _require("mask_area_fraction", fraction,
                 gate["min_mask_area_fraction"], "mask_area_fraction")

    _require("local_contrast", metrics.get("local_contrast"),
             gate["min_local_contrast"], "local_contrast")

    viable = all(checks.values())
    return {
        "viable": viable,
        "gate_version": gate["gate_version"],
        "metrics_version": metrics.get("metrics_version"),
        "reasons": reasons,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Derived cache
# ---------------------------------------------------------------------------

def cache_path(project_path: str | Path, media_type: str = "movie") -> Path:
    """Return the derived viability-metrics cache path."""
    return (
        Path(project_path) / "data" / "indexes" / "viability"
        / f"{media_type}-silhouette-viability.sqlite3"
    )


def canonical_record_path(project_path: str | Path, value: str) -> str:
    """Normalize absolute and relative spellings of one catalog identity."""
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(Path(project_path))
        except ValueError:
            return str(path)
    return path.as_posix()


def _connect(path: Path):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS viability (
            record_path TEXT PRIMARY KEY,
            metrics_version INTEGER NOT NULL,
            metrics TEXT NOT NULL
        )
        """
    )
    return connection


def load_cached_metrics(project_path: str | Path, record_paths: list[str],
                        media_type: str = "movie") -> dict[str, dict]:
    """Return cached metrics for *record_paths* at the current metrics version."""
    path = cache_path(project_path, media_type)
    if not path.is_file():
        return {}
    connection = _connect(path)
    try:
        found: dict[str, dict] = {}
        for chunk_start in range(0, len(record_paths), 500):
            chunk = record_paths[chunk_start:chunk_start + 500]
            normalized = {value: canonical_record_path(project_path, value) for value in chunk}
            placeholders = ",".join("?" * len(chunk))
            rows = connection.execute(
                f"SELECT record_path, metrics FROM viability "
                f"WHERE metrics_version = ? AND record_path IN ({placeholders})",
                [METRICS_VERSION, *normalized.values()],
            )
            indexed = {record_path: json.loads(payload) for record_path, payload in rows}
            for original, key in normalized.items():
                metrics = indexed.get(key)
                if metrics and metrics.get("metrics_version") == METRICS_VERSION:
                    found[original] = metrics
        return found
    finally:
        connection.close()


def store_cached_metrics(project_path: str | Path, entries: dict[str, dict],
                         media_type: str = "movie") -> int:
    """Persist measured metrics; returns the number of rows written."""
    if not entries:
        return 0
    if any(metrics.get("metrics_version") != METRICS_VERSION for metrics in entries.values()):
        raise ValueError("Cannot cache measurements from a different metrics version")
    connection = _connect(cache_path(project_path, media_type))
    try:
        connection.executemany(
            "INSERT OR REPLACE INTO viability (record_path, metrics_version, metrics) "
            "VALUES (?, ?, ?)",
            [
                (canonical_record_path(project_path, record_path), METRICS_VERSION, json.dumps(metrics))
                for record_path, metrics in entries.items()
            ],
        )
        connection.commit()
        return len(entries)
    finally:
        connection.close()


def silhouette_png_for_record(project_path: str | Path, record: dict) -> Path | None:
    """Return the silhouette PNG for a catalog record, if present."""
    raw = record.get("record_path") or record.get("path") or ""
    if not raw:
        return None
    json_path = Path(raw)
    if not json_path.is_absolute():
        json_path = Path(project_path) / json_path
    name = record.get("png") or ""
    if name:
        candidate = json_path.parent / name
        if candidate.is_file():
            return candidate
    candidate = json_path.with_suffix(".png")
    return candidate if candidate.is_file() else None


def measure_records(
    project_path: str | Path,
    records: list[dict],
    *,
    media_type: str = "movie",
    use_cache: bool = True,
    progress=None,
) -> dict[str, dict]:
    """Measure many records, reusing and extending the derived cache."""
    keys = [str(r.get("record_path") or r.get("path") or "") for r in records]
    cached = load_cached_metrics(project_path, keys, media_type) if use_cache else {}

    fresh: dict[str, dict] = {}
    for index, (record, key) in enumerate(zip(records, keys), start=1):
        if not key or key in cached:
            continue
        png = silhouette_png_for_record(project_path, record)
        metrics = measure_silhouette(png) if png else None
        if metrics:
            fresh[key] = metrics
        if progress and index % 500 == 0:
            progress(index, len(records))

    if use_cache and fresh:
        store_cached_metrics(project_path, fresh, media_type)

    return {**cached, **fresh}
