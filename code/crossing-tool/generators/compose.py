"""compose — frame-compositing tool driven by search results.

Operates exclusively on the output of ``search_shots()``; does NOT perform
any internal dataset traversal or metadata queries.

Pipeline:
    1. Select a background result  → extract frame → apply colour treatment
    2. Select foreground results   → extract frames → overlay as patches
    3. Save to <project>/output/compositions/

Canvas presets:
    portrait  — 1240 × 1754 px  (≈ A3 @ 150 dpi)
    landscape — 1920 × 1080 px  (Full HD)

Example::

    from services.search import search_shots
    from generators.compose import compose_from_search_results

    res = search_shots("gun", scopes=None, field=None,
                       limit=20, limit_per_item=None, use_all=True,
                       project_path=project_path)
    out = compose_from_search_results(res["results"], project_path)
"""

from __future__ import annotations

import random
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Canvas presets
# ---------------------------------------------------------------------------

CANVAS_SIZES: dict[str, tuple[int, int]] = {
    "portrait":  (1240, 1754),
    "landscape": (1920, 1080),
}


# ---------------------------------------------------------------------------
# Video helpers
# ---------------------------------------------------------------------------

def _get_sar(video_path: str) -> tuple[int, int]:
    """Return (sar_num, sar_den) from ffprobe; fallback (1, 1)."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=sample_aspect_ratio",
                "-of", "json", video_path,
            ],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout)
        sar_str = data["streams"][0].get("sample_aspect_ratio", "1:1")
        if sar_str in ("", "0:1", "1:1"):
            return (1, 1)
        parts = sar_str.replace("/", ":").split(":")
        return (int(parts[0]), int(parts[1]))
    except Exception:
        return (1, 1)


def _extract_frame(video_path: str, frame_no: int) -> "np.ndarray | None":
    """Return a single BGR frame via cv2, or None."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
    ret, frame = cap.read()
    cap.release()
    return frame if ret else None


def _sar_correct(rgb: "np.ndarray", sar_num: int, sar_den: int) -> "np.ndarray":
    if (sar_num, sar_den) == (1, 1):
        return rgb
    h, w = rgb.shape[:2]
    dw = int(round(w * sar_num / sar_den))
    return cv2.resize(rgb, (dw, h), interpolation=cv2.INTER_LINEAR)


def _frame_count(video_path: str) -> int:
    cap = cv2.VideoCapture(str(video_path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return n


# ---------------------------------------------------------------------------
# Background treatment
# ---------------------------------------------------------------------------


def _apply_bg_treatment(
    img: Image.Image,
    canvas_w: int,
    canvas_h: int,
    rng: random.Random,
    treatment: str | None = None,
) -> Image.Image:
    """Scale-to-fill, center-crop, then apply background treatment.

    Default: convert to greyscale, boost contrast, then overlay a random hue
    at 75 % HSB saturation as a multiplicative tint.
    """
    import colorsys

    src_w, src_h = img.size
    scale = max(canvas_w / src_w, canvas_h / src_h)
    nw, nh = int(round(src_w * scale)), int(round(src_h * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - canvas_w) // 2
    top  = (nh - canvas_h) // 2
    img  = img.crop((left, top, left + canvas_w, top + canvas_h))

    # --- Greyscale + contrast boost ----------------------------------------
    grey = img.convert("L")
    # Auto-contrast: stretch histogram so darkest → 0, brightest → 255
    from PIL import ImageOps, ImageEnhance
    grey = ImageOps.autocontrast(grey, cutoff=2)
    # Additional contrast punch
    grey = ImageEnhance.Contrast(grey).enhance(1.6)

    # --- Random hue tint at 75 % saturation, full brightness ---------------
    hue = rng.uniform(0.0, 1.0)               # 0–1 maps to 0°–360°
    sat = 0.75
    r, g, b = colorsys.hsv_to_rgb(hue, sat, 1.0)
    tint_colour = (int(r * 255), int(g * 255), int(b * 255))

    # Multiply grey by tint: convert grey to RGB then blend
    grey_rgb = grey.convert("RGB")
    tint_layer = Image.new("RGB", grey_rgb.size, tint_colour)
    # PIL multiply: ImageChops.multiply does (a*b)/255 per channel
    from PIL import ImageChops
    img = ImageChops.multiply(grey_rgb, tint_layer)

    return img.convert("RGBA")


# ---------------------------------------------------------------------------
# Foreground overlay
# ---------------------------------------------------------------------------

def _overlay_frame(
    canvas: "Image.Image",
    frame_rgb: "np.ndarray",
    rng: random.Random,
    canvas_w: int,
    canvas_h: int,
) -> None:
    """Scale, rotate, and semi-transparently paste a video frame onto *canvas*.

    The patch is treated as a rectangular RGBA image; no segmentation mask is
    applied (that is the role of a future SAM integration step).
    """
    patch = Image.fromarray(frame_rgb).convert("RGBA")
    scale = rng.uniform(0.25, 0.55)
    angle = rng.uniform(-30.0, 30.0)
    opacity = rng.uniform(0.45, 0.85)

    nw = max(8, int(patch.width  * scale))
    nh = max(8, int(patch.height * scale))
    patch = patch.resize((nw, nh), Image.LANCZOS)
    patch = patch.rotate(angle, expand=True, resample=Image.BICUBIC)

    if opacity < 1.0:
        r, g, b, a = patch.split()
        a = a.point(lambda x: int(x * opacity))
        patch = Image.merge("RGBA", (r, g, b, a))

    cx = rng.randint(-patch.width  // 4, canvas_w + patch.width  // 4)
    cy = rng.randint(-patch.height // 4, canvas_h + patch.height // 4)
    canvas.paste(patch, (cx - patch.width // 2, cy - patch.height // 2), patch)


# ---------------------------------------------------------------------------
# Search-result consumer
# ---------------------------------------------------------------------------

def _find_video_path(project_path: str, movie_id: str) -> "Path | None":
    """Locate the video file whose stem matches *movie_id*.

    Searches both ``media/videos/movies/`` and ``media/videos/gameplay/``.
    Returns the first exact-stem match or None.
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


def compose_from_search_results(
    results: list[dict],
    project_path: str,
    *,
    orientation: str = "portrait",
    seed: int | None = None,
    output_path: str | None = None,
    fmt: str = "jpg",
    open_result: bool = True,
) -> Path:
    """Compose a poster-style image from ``search_shots()`` results.

    Does NOT perform any internal search or dataset traversal.
    Callers must supply a non-empty *results* list.

    Pipeline:
        1. Shuffle results with *seed* for reproducibility.
        2. First result  → background frame → apply colour treatment.
        3. Remaining results (up to 12) → overlaid as scaled rectangular
           frame patches with random position, rotation, and opacity.
        4. Save to ``<project>/output/compositions/``.

    Args:
        results:      Non-empty list from ``search_shots()["results"]``.
        project_path: Project root directory.
        orientation:  ``"portrait"`` or ``"landscape"``.
        seed:         RNG seed for reproducibility (random if None).
        output_path:  Destination file (auto-generated if not provided).
        fmt:          ``"jpg"`` or ``"pdf"``.
        open_result:  Open the saved file with ``xdg-open``.

    Returns:
        Path to the saved composition file.

    Raises:
        ValueError:  If *results* is empty or no video frames can be loaded.

    Example
    -------
        from services.search import search_shots
        from generators.compose import compose_from_search_results

        res = search_shots("gun", scopes=None, field=None, limit=20,
                           limit_per_item=None, use_all=True,
                           project_path=project_path)
        out = compose_from_search_results(res["results"], project_path)
    """
    import datetime
    import subprocess

    if not results:
        raise ValueError("compose_from_search_results: results list is empty")

    rng = random.Random(seed)

    # ---- Canvas ----------------------------------------------------------------
    canvas_w, canvas_h = CANVAS_SIZES.get(orientation, CANVAS_SIZES["portrait"])

    # ---- Shuffle results for random selection ----------------------------------
    pool = list(results)
    rng.shuffle(pool)

    # ---- Background: first result in shuffled pool ----------------------------
    bg_frame_rgb: np.ndarray | None = None
    bg_result = None
    remaining: list[dict] = []

    for i, r in enumerate(pool):
        video_path = _find_video_path(project_path, r.get("movie_id", ""))
        if video_path is None:
            remaining.append(r)
            continue
        frame_no = r.get("start_frame") or 0
        bgr = _extract_frame(str(video_path), int(frame_no))
        if bgr is None:
            remaining.append(r)
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        rgb = _sar_correct(rgb, *_get_sar(str(video_path)))
        bg_frame_rgb = rgb
        bg_result = r
        remaining = pool[i + 1:]
        break

    if bg_frame_rgb is None:
        raise ValueError(
            "compose_from_search_results: could not load any background frame — "
            "check that video files are present for the matched movies."
        )

    # ---- Build background canvas ----------------------------------------------
    canvas = _apply_bg_treatment(
        Image.fromarray(bg_frame_rgb), canvas_w, canvas_h, rng
    )

    # ---- Foreground overlays --------------------------------------------------
    MAX_FG = 12
    placed = 0
    for r in remaining[:MAX_FG]:
        video_path = _find_video_path(project_path, r.get("movie_id", ""))
        if video_path is None:
            continue
        frame_no = r.get("start_frame") or 0
        bgr = _extract_frame(str(video_path), int(frame_no))
        if bgr is None:
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        rgb = _sar_correct(rgb, *_get_sar(str(video_path)))
        _overlay_frame(canvas, rgb, rng, canvas_w, canvas_h)
        placed += 1

    # ---- Save -----------------------------------------------------------------
    out_dir = Path(project_path) / "output" / "compositions"
    out_dir.mkdir(parents=True, exist_ok=True)

    if output_path:
        out_path = Path(output_path)
    else:
        stamp    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        seed_str = str(seed) if seed is not None else "rnd"
        out_path = out_dir / f"composition_search_{stamp}_{seed_str}.{fmt}"

    final = canvas.convert("RGB") if fmt in ("jpg", "jpeg") else canvas
    if fmt == "pdf":
        final.save(str(out_path), "PDF", resolution=150)
    else:
        final.save(str(out_path), "JPEG", quality=93)

    if open_result:
        try:
            import subprocess as _sp
            _sp.Popen(["xdg-open", str(out_path)])
        except Exception:
            pass

    return out_path
