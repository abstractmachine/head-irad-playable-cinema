"""
services/frame_retrieval.py — Direct frame retrieval for MCP multimodal tools and CLI.

Provides read-only frame extraction with in-memory JPEG encoding.
All functions return plain dicts — the MCP layer wraps these into FastMCP
Image objects; the CLI layer saves them to disk or prints metadata.

Frame sourcing priority (for each shot):
  1. Pre-computed best-frame PNG cache  (media/frames/best/<media_type>/<stem>/<shot_id>.png)
     — zero video I/O, CLIP-selected representative frame
  2. Video extraction via OpenCV        (media/videos/<media_type>/<stem>.*)
     — fallback when the best-frame cache is absent

Output: JPEG bytes at requested *width* (default 400 px), quality 75.
At 400 px a typical cinematic frame is ~30–80 KB.  4 frames ≈ 150–300 KB,
well within the 1 MB MCP transport limit.

Each public function returns a list of dicts with these keys:
    film_title  : str   — resolved film title
    filename    : str   — video filename (stem+ext) used for path construction
    shot_id     : str   — canonical shot identifier
    start_time  : str   — HH:MM:SS.mmm start timecode (may be empty)
    end_time    : str   — HH:MM:SS.mmm end timecode (may be empty)
    image_data  : bytes — JPEG bytes ready for base64 encoding
    mime_type   : str   — always "image/jpeg"
    metadata    : dict  — function-specific extra fields
"""

from __future__ import annotations

import io
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_WIDTH   = 400    # pixels — longest side of thumbnail
_DEFAULT_QUALITY = 75     # JPEG quality (integer 1-95)
_MAX_PAYLOAD     = 900_000  # 900 KB safety cap (per batch)


# ---------------------------------------------------------------------------
# Internal image helpers
# ---------------------------------------------------------------------------

def _resize_pil(img, width: int):
    """Scale *img* so its longest side equals *width*.

    Returns *img* unchanged if *width* ≤ 0 or the image is already smaller.
    """
    from PIL import Image as PILImage
    if width <= 0:
        return img
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    scale = width / max(w, h)
    if scale >= 1.0:
        return img
    return img.resize(
        (max(1, int(w * scale)), max(1, int(h * scale))),
        PILImage.LANCZOS,
    )


def _pil_to_jpeg_bytes(img, quality: int = _DEFAULT_QUALITY) -> bytes:
    """Encode a PIL image as JPEG bytes in memory."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


_VIDEO_EXTS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".m4v",
    ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".ts",
})


def _normalise_film_query(film: str) -> str:
    """Strip a video extension from *film* so get_metadata substring-matching works.

    Stored filenames look like ``"Django (1966) {tmdb-4638}.mp4"``.
    A query of ``"Django (1966).mp4"`` fails to match because ``.mp4`` terminates
    before the ``{tmdb-...}`` suffix in the stored name.
    Stripping the extension gives ``"Django (1966)"`` which IS a substring match.
    """
    suffix = Path(film).suffix.lower()
    if suffix in _VIDEO_EXTS:
        return Path(film).stem
    return film


def _fetch_frame(
    project_path: str,
    media_type: str,
    filename: str,
    shot_id: str,
    start_frame,
    end_frame,
    best_frame_idx=None,
    *,
    width: int = _DEFAULT_WIDTH,
    quality: int = _DEFAULT_QUALITY,
) -> bytes | None:
    """Return JPEG thumbnail bytes for one shot, or None on failure.

    Frame sourcing order:
      1. Best-frame PNG cache at media/frames/best/<media_type>/<stem>/<shot_id>.png
      2. Video extraction at the midpoint (or best_frame_idx) of the shot

    Parameters
    ----------
    filename :
        Video filename (with or without extension, with or without ``{tmdb-...}`` suffix).
        ``best_frame_path`` strips the extension internally; ``_find_video_path``
        receives ``Path(filename).stem`` to match the on-disk video file.
    """
    from PIL import Image as PILImage

    # --- Priority 1: pre-computed best-frame PNG cache ---
    from services.frame_match import best_frame_path as _bf_path
    bf_png = _bf_path(project_path, media_type, filename, shot_id)
    if bf_png.exists():
        try:
            img = PILImage.open(bf_png).convert("RGB")
            return _pil_to_jpeg_bytes(_resize_pil(img, width), quality)
        except Exception:
            pass  # fall through to video extraction

    # --- Priority 2: video extraction ---
    if start_frame is None:
        return None

    from generators.mosaic import _find_video_path, extract_frame_pil, frame_from_pct

    video_path = _find_video_path(project_path, Path(filename).stem)
    if video_path is None:
        return None

    if best_frame_idx is not None:
        frame_index = int(best_frame_idx)
    elif end_frame is not None:
        frame_index = frame_from_pct(int(start_frame), int(end_frame), 0.5)
    else:
        frame_index = int(start_frame)

    img = extract_frame_pil(video_path, frame_index)
    if img is None:
        return None

    return _pil_to_jpeg_bytes(_resize_pil(img, width), quality)


# ---------------------------------------------------------------------------
# Result dict factory
# ---------------------------------------------------------------------------

def _make_result(
    film_title: str,
    filename: str,
    shot_id: str,
    image_data: bytes,
    start_time: str = "",
    end_time: str = "",
    metadata: dict | None = None,
) -> dict:
    return {
        "film_title": film_title,
        "filename":   filename,
        "shot_id":    shot_id,
        "start_time": start_time,
        "end_time":   end_time,
        "image_data": image_data,
        "mime_type":  "image/jpeg",
        "metadata":   metadata or {},
    }


# ---------------------------------------------------------------------------
# Public retrieval functions
# ---------------------------------------------------------------------------

def retrieve_single_frame(
    project_path: str,
    film: str,
    shot_id: str,
    media_type: str = "movies",
    *,
    width: int = _DEFAULT_WIDTH,
    quality: int = _DEFAULT_QUALITY,
) -> dict:
    """Retrieve one frame for a specific shot.

    Returns a single result dict.
    Raises ValueError if the film/shot is not found or no frame can be extracted.
    """
    from data.metadata import get_metadata as _get_metadata
    from data.shotlist import read_shotlist

    film = _normalise_film_query(film)
    entries = _get_metadata(project_path, query=film, media_type=media_type)
    if not entries:
        raise ValueError(f"No film found matching {film!r}.")
    if len(entries) > 1:
        titles = [e.get("title", e.get("filename", "")) for e in entries]
        raise ValueError(f"Ambiguous: {len(entries)} films match {film!r}. Matches: {titles}")

    entry    = entries[0]
    filename = entry["filename"]
    title    = entry.get("title", Path(filename).stem)

    shots     = read_shotlist(project_path, filename, media_type)
    shot_data = next((s for s in shots if s.get("shot_id") == shot_id), None)
    if shot_data is None:
        raise ValueError(f"Shot {shot_id!r} not found in shotlist for {filename!r}.")

    image_data = _fetch_frame(
        project_path, media_type, filename, shot_id,
        shot_data.get("start_frame"), shot_data.get("end_frame"),
        width=width, quality=quality,
    )
    if image_data is None:
        raise ValueError(
            f"Could not retrieve frame for shot {shot_id!r} in {filename!r}. "
            "No best-frame cache found and video not available."
        )

    return _make_result(
        title, filename, shot_id, image_data,
        start_time=shot_data.get("start_time", ""),
        end_time=shot_data.get("end_time", ""),
    )


def retrieve_frames_for_query(
    project_path: str,
    query: str,
    *,
    films: list[str] | None = None,
    field: str | None = None,
    limit: int = 4,
    media_type: str = "movies",
    width: int = _DEFAULT_WIDTH,
    quality: int = _DEFAULT_QUALITY,
) -> list[dict]:
    """Retrieve frames for shots matching a text query.

    Runs search_shots() then extracts one thumbnail per result.
    Returns up to *limit* result dicts.
    """
    from services.search import search_shots

    use_all = not films
    res = search_shots(
        query=query,
        scopes=films or [],
        field=field or None,
        limit=limit,
        limit_per_item=None,
        use_all=use_all,
        project_path=project_path,
        media_type=media_type,
    )

    results: list[dict] = []
    cumulative = 0

    for r in res.get("results", []):
        filename   = r.get("filename") or r.get("movie_id", "")
        shot_id    = r.get("shot_id", "")
        film_title = r.get("movie_title", Path(filename).stem)

        image_data = _fetch_frame(
            project_path, media_type, filename, shot_id,
            r.get("start_frame"), r.get("end_frame"),
            r.get("best_frame"),
            width=width, quality=quality,
        )
        if image_data is None:
            continue

        cumulative += len(image_data)
        if cumulative > _MAX_PAYLOAD:
            break

        results.append(_make_result(
            film_title, filename, shot_id, image_data,
            start_time=r.get("start_time", ""),
            end_time=r.get("end_time", ""),
            metadata={
                "field": r.get("field", ""),
                "value": r.get("value", ""),
                "score": r.get("score"),
                "query": query,
            },
        ))

    return results


def retrieve_palette_frames(
    project_path: str,
    *,
    films: list[str] | None = None,
    media_type: str = "movies",
    limit: int = 4,
    width: int = _DEFAULT_WIDTH,
    quality: int = _DEFAULT_QUALITY,
    warm: bool = False,
    cold: bool = False,
    dark: bool = False,
    bright: bool = False,
    low_chroma: bool = False,
    high_chroma: bool = False,
    foreground_only: bool = False,
    background_only: bool = False,
    luminance_min: float | None = None,
    luminance_max: float | None = None,
    chroma_min: float | None = None,
    chroma_max: float | None = None,
) -> list[dict]:
    """Retrieve frames matching palette colour filters.

    Uses search_palette() then extracts one thumbnail per unique shot
    (deduplicating shots that match both foreground and background regions).
    Returns up to *limit* result dicts.
    """
    from services.analysis import search_palette
    from data.metadata import get_metadata as _get_metadata

    all_entries = _get_metadata(project_path, media_type=media_type)

    # Request extra results to account for deduplication across fg/bg regions.
    palette_res = search_palette(
        project_path, media_type, films,
        warm=warm, cold=cold, dark=dark, bright=bright,
        low_chroma=low_chroma, high_chroma=high_chroma,
        foreground_only=foreground_only, background_only=background_only,
        luminance_min=luminance_min, luminance_max=luminance_max,
        chroma_min=chroma_min, chroma_max=chroma_max,
        limit=limit * 3,
    )

    seen_shots: set[str] = set()
    results: list[dict] = []
    cumulative = 0

    for r in palette_res.get("results", []):
        filename = r.get("filename", "")
        shot_id  = r.get("shot_id", "")
        key = f"{filename}::{shot_id}"
        if key in seen_shots:
            continue  # deduplicate shots appearing for both fg and bg
        seen_shots.add(key)

        film_title = r.get("film", Path(filename).stem)

        image_data = _fetch_frame(
            project_path, media_type, filename, shot_id,
            r.get("start_frame"), r.get("end_frame"),
            width=width, quality=quality,
        )
        if image_data is None:
            continue

        cumulative += len(image_data)
        if cumulative > _MAX_PAYLOAD:
            break

        results.append(_make_result(
            film_title, filename, shot_id, image_data,
            start_time=r.get("start_time", ""),
            end_time=r.get("end_time", ""),
            metadata={
                "region":    r.get("region", ""),
                "luminance": r.get("luminance"),
                "chroma":    r.get("chroma"),
                "rgb":       r.get("rgb"),
                "lab":       r.get("lab"),
            },
        ))

        if len(results) >= limit:
            break

    return results


def retrieve_motif_frames(
    project_path: str,
    motif: str,
    *,
    films: list[str] | None = None,
    media_type: str = "movies",
    limit: int = 4,
    width: int = _DEFAULT_WIDTH,
    quality: int = _DEFAULT_QUALITY,
) -> list[dict]:
    """Retrieve frames for shots whose motif word matches *motif*.

    Exact match is tried first; falls back to substring match if exact
    yields fewer than *limit* results.
    Returns up to *limit* result dicts.
    """
    from data.metadata import get_metadata as _get_metadata
    from data.motif import load_motif_doc, get_motif_path
    from data.shotlist import read_shotlist

    all_entries = _get_metadata(project_path, media_type=media_type)

    if films:
        # Filter entries by film list (case-insensitive substring or exact match).
        film_lower = [f.lower() for f in films]
        selected = [
            e for e in all_entries
            if any(
                fl in (e.get("title", "") or "").lower()
                or fl in (e.get("filename", "") or "").lower()
                for fl in film_lower
            )
        ]
        if not selected:
            selected = all_entries
    else:
        selected = all_entries

    motif_lower = motif.strip().lower()
    results: list[dict] = []
    cumulative = 0

    for entry in selected:
        filename = entry.get("filename", "")
        if not filename:
            continue
        if not get_motif_path(project_path, filename, media_type).exists():
            continue

        film_title = entry.get("title", Path(filename).stem)

        doc = load_motif_doc(project_path, filename, media_type)

        # Build shotlist timing lookup.
        timing_by_id: dict[str, dict] = {}
        try:
            for row in read_shotlist(project_path, filename, media_type):
                sid = row.get("shot_id", "")
                if sid:
                    timing_by_id[sid] = row
        except FileNotFoundError:
            pass

        for shot_entry in doc.get("shots", []):
            value = shot_entry.get("value", "")
            if not value:
                continue
            if value.lower() != motif_lower and motif_lower not in value.lower():
                continue

            shot_id = shot_entry.get("shot_id", "")
            timing  = timing_by_id.get(shot_id, {})

            image_data = _fetch_frame(
                project_path, media_type, filename, shot_id,
                timing.get("start_frame"), timing.get("end_frame"),
                width=width, quality=quality,
            )
            if image_data is None:
                continue

            cumulative += len(image_data)
            if cumulative > _MAX_PAYLOAD:
                return results

            results.append(_make_result(
                film_title, filename, shot_id, image_data,
                start_time=timing.get("start_time", ""),
                end_time=timing.get("end_time", ""),
                metadata={
                    "motif": value,
                    "score": shot_entry.get("score"),
                    "model": shot_entry.get("model"),
                },
            ))

            if len(results) >= limit:
                return results

    return results


def retrieve_context_frames(
    project_path: str,
    film: str,
    shot_id: str,
    *,
    window: int = 3,
    media_type: str = "movies",
    width: int = _DEFAULT_WIDTH,
    quality: int = _DEFAULT_QUALITY,
) -> list[dict]:
    """Retrieve frames for a central shot and its neighbors.

    Uses get_shot_context() for the window, then extracts one thumbnail
    per shot.  The central shot is flagged with ``metadata["is_center"] = True``.
    Returns a list of result dicts (up to 2*window+1 entries).
    """
    from services.analysis import get_shot_context
    from data.metadata import get_metadata as _get_metadata

    film = _normalise_film_query(film)
    ctx = get_shot_context(
        project_path, film, shot_id,
        media_type=media_type, window=window,
    )

    # Resolve the entry to get the full filename for frame extraction.
    entries = _get_metadata(project_path, query=film, media_type=media_type)
    entry      = entries[0] if entries else {}
    filename   = ctx.get("filename", entry.get("filename", ""))
    film_title = ctx.get("film", entry.get("title", Path(filename).stem))
    center_id  = ctx.get("shot_id", shot_id)

    results: list[dict] = []
    cumulative = 0

    for i, shot in enumerate(ctx.get("shots", [])):
        sid       = shot.get("shot_id", "")
        is_center = sid == center_id

        image_data = _fetch_frame(
            project_path, media_type, filename, sid,
            shot.get("start_frame"), shot.get("end_frame"),
            width=width, quality=quality,
        )
        if image_data is None:
            continue

        cumulative += len(image_data)
        if cumulative > _MAX_PAYLOAD:
            break

        results.append(_make_result(
            film_title, filename, sid, image_data,
            start_time=shot.get("start_time", ""),
            end_time=shot.get("end_time", ""),
            metadata={
                "is_center": is_center,
                "scene":     shot.get("Scene", ""),
                "position":  i,
            },
        ))

    return results
