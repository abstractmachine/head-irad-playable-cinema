"""Palette indexing: dominant foreground/background colour extraction from best frames.

Cache path
----------
    <project>/data/index/palette/<media_type>/<stem>.json

For each shot in a film the best-frame PNG (written by ``crossing annotate
frame``) is loaded, resized to 128×128 and split into two non-overlapping
regions.  The dominant colour in each region is returned as an RGB triplet.

Algorithm
---------
1. Load the PNG with Pillow; convert to RGB.
2. Resize to 128×128 (deterministic, LANCZOS).
3. Split into two non-overlapping regions:
   - border  : outer quarter (top/bottom/left/right strips, 32 px thick)
   - center  : inner half   (rows 32–96, cols 32–96)
4. For each region compute the dominant colour:
   a. Quantise each channel to 8 bins (right-shift by 5 → values 0–7).
   b. Find the most-common (r, g, b) bin.
   c. Return the mean RGB of all pixels that fall in that bin,
      rounded to the nearest integer.
5. background = dominant colour from border region
   foreground = dominant colour from center region

No random state, no model, no network — output is fully deterministic.

Dependencies: Pillow (core dep), numpy (core dep).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data.index import load_annotation_items
from data.media_id import build_shot_id, compute_media_id
from data.metadata import get_metadata
from data.shotlist import read_shotlist

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RESIZE = 128          # target size after resize
_BORDER = _RESIZE // 4  # 32-pixel outer strip


# ---------------------------------------------------------------------------
# Cache path helpers
# ---------------------------------------------------------------------------

def get_palette_path(project_path: str, filename: str, media_type: str) -> Path:
    """Return the canonical cache path for a palette JSON.

    ``<project>/data/index/palette/<media_type>/<stem>.json``
    """
    stem = Path(filename).stem
    return Path(project_path) / "data" / "index" / "palette" / media_type / f"{stem}.json"


def load_palette(project_path: str, filename: str, media_type: str) -> dict | None:
    """Load a cached palette JSON or return ``None`` if absent."""
    path = get_palette_path(project_path, filename, media_type)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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


# ---------------------------------------------------------------------------
# Colour extraction
# ---------------------------------------------------------------------------

def _extract_dominant_colour(pixels: "np.ndarray") -> tuple[int, int, int]:
    """Return the dominant colour from an ``(N, 3)`` uint8 RGB pixel array.

    Steps:
    1. Quantise each channel to 8 bins by right-shifting 5 bits.
    2. Encode each pixel's bin as a single integer: r*64 + g*8 + b.
    3. Find the bin with the highest count.
    4. Average the original (non-quantised) colours of pixels in that bin.

    Returns a 3-tuple of ints in 0–255.
    Falls back to ``(0, 0, 0)`` for an empty array.
    """
    import numpy as np

    if len(pixels) == 0:
        return (0, 0, 0)

    bins = (pixels >> 5).astype(np.int32)  # 0–7 per channel
    codes = bins[:, 0] * 64 + bins[:, 1] * 8 + bins[:, 2]

    unique, counts = np.unique(codes, return_counts=True)
    best_code = unique[np.argmax(counts)]

    mask = codes == best_code
    mean = pixels[mask].mean(axis=0)
    return (int(round(mean[0])), int(round(mean[1])), int(round(mean[2])))


def extract_fg_bg(
    image_path: Path,
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Extract dominant foreground and background colours from a frame image.

    Returns ``(foreground_rgb, background_rgb)`` as 3-tuples of ints.

    Raises:
        ImportError: If Pillow is not installed.
        OSError: If the image file cannot be opened.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow is required for palette extraction.  "
            "Install with:  pip install Pillow"
        ) from exc

    import numpy as np

    img = (
        Image.open(image_path)
        .convert("RGB")
        .resize((_RESIZE, _RESIZE), Image.LANCZOS)
    )
    arr = np.asarray(img)  # (128, 128, 3) uint8

    # Build boolean masks for border and center regions
    border_mask = np.zeros((_RESIZE, _RESIZE), dtype=bool)
    border_mask[:_BORDER, :] = True
    border_mask[_RESIZE - _BORDER :, :] = True
    border_mask[:, :_BORDER] = True
    border_mask[:, _RESIZE - _BORDER :] = True

    center_mask = ~border_mask  # inner 50%

    border_pixels = arr[border_mask]   # (N, 3)
    center_pixels = arr[center_mask]   # (M, 3)

    background = _extract_dominant_colour(border_pixels)
    foreground = _extract_dominant_colour(center_pixels)

    return foreground, background


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
        foreground, background = extract_fg_bg(png_path)
    except Exception as exc:
        return {**base, "status": "error", "reason": str(exc)}

    return {
        **base,
        "foreground": {"rgb": list(foreground)},
        "background": {"rgb": list(background)},
        "method": "border_center_dominant",
        "confidence": 1.0,
        "status": "ok",
    }


# ---------------------------------------------------------------------------
# Movie-level palette creation
# ---------------------------------------------------------------------------

def create_palette_for_movie(
    project_path: str,
    filename: str,
    media_type: str = "movies",
    *,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Build and cache a palette for every shot in *filename*.

    Reads the annotation JSON and shotlist; for each shot that has a
    ``best_frame`` PNG, extracts foreground and background colours.

    Returns a summary dict with keys:
        ``filename``, ``shot_count``, ``processed``, ``skipped``, ``failed``

    Raises:
        FileExistsError: If the palette cache already exists and *force* is False.
        FileNotFoundError: If the annotation JSON does not exist.
    """
    cache_path = get_palette_path(project_path, filename, media_type)
    if cache_path.exists() and not force:
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

    for i, entry in enumerate(entries):
        shot_data = entry.get("shot", {})
        shot_id = str(shot_data.get("shot_id", ""))
        shot_info = shots_by_id.get(shot_id)

        result = _process_one_shot(
            project_path, filename, media_type, entry, i, shot_info
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
            print(f"  fail  {filename} shot {i}: {reason}", flush=True)

        shot_palettes.append(result)

    summary = {
        "shot_count": len(entries),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
    }

    palette_doc: dict[str, Any] = {
        "movie": movie_block,
        "source": "best_frame",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "shots": shot_palettes,
        "summary": summary,
    }

    save_palette(project_path, filename, media_type, palette_doc, force=force)

    if verbose:
        print(
            f"  → saved  {cache_path.relative_to(project_path) if project_path else cache_path}"
        )

    return {"filename": filename, "cached": False, **summary}


def create_palette_for_all_movies(
    project_path: str,
    media_type: str = "movies",
    *,
    force: bool = False,
    verbose: bool = False,
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
            continue
        except FileExistsError as exc:
            # Should not happen because we handle cache-exists inside
            # create_palette_for_movie, but guard here anyway.
            print(f"  skip  {filename}: {exc}", flush=True)
            results.append({"filename": filename, "skipped": True, "reason": str(exc)})
            continue
        except Exception as exc:
            print(f"  fail  {filename}: {exc}", flush=True)
            results.append({"filename": filename, "error": str(exc)})
            continue

        if summary.get("cached"):
            total_cached += 1
        else:
            total_processed += summary.get("processed", 0)
            total_skipped += summary.get("skipped", 0)
            total_failed += summary.get("failed", 0)
        results.append(summary)

    return {
        "media_type": media_type,
        "total_files": len(filenames),
        "total_processed": total_processed,
        "total_skipped": total_skipped,
        "total_failed": total_failed,
        "total_cached": total_cached,
        "results": results,
    }
