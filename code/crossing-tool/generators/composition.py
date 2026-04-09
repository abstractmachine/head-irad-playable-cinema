"""composition — minimal one-layer tableau composer.

Pipeline:
    1. Caller passes ``search_shots()`` results.
    2. ``choose_background()``  — pick one random result from the result set.
    3. ``build_tableau()``      — extract the frame, fit it to the canvas.
    4. ``save_tableau()``       — write a JPEG named ``<criteria>+<date>+<time>.jpg``.

Canvas presets:
    portrait  — 1240 × 1754 px  (≈ A3 @ 150 dpi, poster-like)
    landscape — 1920 × 1080 px  (Full HD, cinemascope-like)

Example::

    from services.search import search_shots
    from generators.composition import choose_background, build_tableau, save_tableau

    res = search_shots("gun", scopes=None, field=None,
                       limit=None, limit_per_item=None, use_all=True,
                       project_path=project_path)
    result  = choose_background(res["results"])
    tableau = build_tableau(result, project_path, orientation="portrait")
    out     = save_tableau(tableau, "gun", Path(project_path) / "output" / "compositions")
"""

from __future__ import annotations

import datetime
import random
import re
from pathlib import Path

from PIL import Image


# ---------------------------------------------------------------------------
# Canvas presets
# ---------------------------------------------------------------------------

CANVAS_SIZES: dict[str, tuple[int, int]] = {
    "portrait":  (1240, 1754),
    "landscape": (1920, 1080),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_video_path(project_path: str, movie_id: str) -> Path | None:
    """Locate the video file whose stem matches *movie_id*.

    Searches subdirectories of ``<project>/media/videos/``.
    Returns the first match or None.
    """
    videos_base = Path(project_path) / "media" / "videos"
    if not videos_base.exists():
        return None
    for subdir in sorted(videos_base.iterdir()):
        if not subdir.is_dir():
            continue
        for f in subdir.iterdir():
            if f.is_file() and f.stem == movie_id:
                return f
    return None


def _extract_frame_pil(video_path: Path, frame_index: int) -> Image.Image | None:
    """Extract a single video frame as a PIL Image (RGB). Returns None on failure."""
    try:
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ret, bgr = cap.read()
        cap.release()
        if not ret:
            return None
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb)
    except Exception:
        return None


def _fit_to_fill(img: Image.Image, canvas_w: int, canvas_h: int) -> Image.Image:
    """Scale-to-fill then center-crop to exactly ``canvas_w × canvas_h``."""
    src_w, src_h = img.size
    scale = max(canvas_w / src_w, canvas_h / src_h)
    nw = max(1, int(round(src_w * scale)))
    nh = max(1, int(round(src_h * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - canvas_w) // 2
    top  = (nh - canvas_h) // 2
    return img.crop((left, top, left + canvas_w, top + canvas_h))


def _sanitize_criteria(criteria: str) -> str:
    """Sanitize a search criteria string for use in a filename."""
    s = criteria.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "_", s)
    return s[:64] or "compose"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def choose_background(results: list[dict], seed: int | None = None) -> dict:
    """Pick one random result from *results*.

    Args:
        results: Non-empty list from ``search_shots()["results"]``.
        seed:    RNG seed for reproducibility (random if None).

    Returns:
        One result dict.

    Raises:
        ValueError: If *results* is empty.
    """
    if not results:
        raise ValueError("choose_background: results list is empty")
    rng = random.Random(seed)
    return rng.choice(results)


def build_tableau(
    result: dict,
    project_path: str,
    orientation: str = "portrait",
) -> Image.Image:
    """Extract the frame for *result* and fit it to the tableau canvas.

    Args:
        result:       A single result dict from ``search_shots()["results"]``.
        project_path: Project root directory.
        orientation:  ``"portrait"`` or ``"landscape"``.

    Returns:
        A PIL Image (RGB) at the canvas size.

    Raises:
        ValueError: If the video file or frame cannot be resolved.
    """
    canvas_w, canvas_h = CANVAS_SIZES.get(orientation, CANVAS_SIZES["portrait"])

    movie_id   = result.get("movie_id", "")
    video_path = _find_video_path(project_path, movie_id)
    if video_path is None:
        raise ValueError(f"build_tableau: video not found for movie_id={movie_id!r}")

    sf = result.get("start_frame")
    ef = result.get("end_frame")
    if sf is not None and ef is not None:
        frame_index = int(sf + (ef - sf) * 0.5)
    elif sf is not None:
        frame_index = int(sf)
    else:
        frame_index = 0

    img = _extract_frame_pil(video_path, frame_index)
    if img is None:
        raise ValueError(
            f"build_tableau: could not extract frame {frame_index} from {video_path}"
        )

    return _fit_to_fill(img.convert("RGB"), canvas_w, canvas_h)


def save_tableau(
    img: Image.Image,
    criteria: str,
    output_dir: Path,
) -> Path:
    """Save *img* as JPEG named ``<criteria>+<date>+<time>.jpg``.

    Args:
        img:        PIL Image (RGB) to save.
        criteria:   The search criteria string used to generate the tableau.
        output_dir: Destination directory (created if absent).

    Returns:
        Path to the saved file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    now      = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H-%M-%S")
    name     = f"{_sanitize_criteria(criteria)}+{date_str}+{time_str}.jpg"
    out_path = output_dir / name
    img.convert("RGB").save(str(out_path), "JPEG", quality=93)
    return out_path
