"""Mosaic renderer — assembles a grid of images into a single contact sheet.

Architecture:
  1. Source collection: callers create MosaicItem instances with either a
     static image path (thumbnails) or a video path + frame index (video frames).
  2. Rendering: render_mosaic() resolves each item to a PIL Image, computes a
     grid layout, and saves one composite PNG.

Typical use:
    items = [MosaicItem(image_path=p, caption="Title") for p in paths]
    render_mosaic(items, Path("output/mosaics/movies-thumbnails-mosaic.png"))
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from generators._common import FONTS_DIR as _FONTS_DIR, load_font_with_fallback


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MosaicItem:
    """A single cell in the mosaic grid.

    Either image_path, (video_path + frame_index), or pil_image must be set.
    caption is shown below the tile if show_captions=True.

    Optional crop:
        crop_bbox     [x1, y1, x2, y2] pixel region within the full frame to
                      show.  When set, only that region is rendered.
        crop_padding  Extra pixels of context added around crop_bbox on all
                      sides (clamped to frame bounds). Default 20.
    """
    image_path: Path | None = None
    video_path: Path | None = None
    frame_index: int | None = None
    pil_image: "Image.Image | None" = None  # pre-rendered PIL image (takes priority)
    caption: str | None = None
    metadata: dict[str, Any] | None = None
    crop_bbox: list[int] | None = None   # [x1, y1, x2, y2]
    crop_padding: int = 20               # context pixels around the crop


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

@lru_cache(maxsize=128)
def _get_sar(video_path: str) -> tuple:
    """Return (sar_num, sar_den) for video_path via ffprobe. Falls back to (1, 1)."""
    try:
        import json
        import subprocess
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=sample_aspect_ratio",
                "-of", "json",
                video_path,
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


def extract_frame_pil(video_path: Path, frame_index: int) -> "Image.Image | None":
    """Extract a single video frame as a PIL Image.

    Uses cv2 (OpenCV) for fast random-access frame seeking.
    Returns None if the frame cannot be read for any reason (missing file,
    codec error, out-of-range index, missing cv2 package).
    """
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
        sar = _get_sar(str(video_path))
        if sar != (1, 1):
            display_w = int(round(rgb.shape[1] * sar[0] / sar[1]))
            rgb = cv2.resize(rgb, (display_w, rgb.shape[0]), interpolation=cv2.INTER_LINEAR)
        return Image.fromarray(rgb)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------

def _compute_grid(n: int, layout: str = "landscape") -> tuple[int, int]:
    """Return (cols, rows) for n items.

    layout="landscape" → wider grid (more cols) — good for video frames
    layout="portrait"  → taller grid (more rows) — good for portrait posters
    """
    if n <= 0:
        return (0, 0)
    # Bias the aspect ratio of the grid toward the named orientation.
    if layout == "portrait":
        cols = max(1, int(math.ceil(math.sqrt(n * 0.65))))
    else:  # landscape
        cols = max(1, int(math.ceil(math.sqrt(n * 1.5))))
    rows = max(1, int(math.ceil(n / cols)))
    return cols, rows


def _load_item_image(item: MosaicItem) -> "Image.Image | None":
    """Resolve a MosaicItem to a PIL Image (RGB), optionally cropped."""
    if item.pil_image is not None:
        return item.pil_image.convert("RGB")
    if item.image_path is not None:
        try:
            img: Image.Image | None = Image.open(item.image_path).convert("RGB")
        except Exception:
            return None
    elif item.video_path is not None and item.frame_index is not None:
        img = extract_frame_pil(item.video_path, item.frame_index)
    else:
        return None

    if img is None:
        return None

    if item.crop_bbox is not None:
        x1, y1, x2, y2 = item.crop_bbox
        pad = item.crop_padding
        fw, fh = img.size
        x1c = max(0, x1 - pad)
        y1c = max(0, y1 - pad)
        x2c = min(fw, x2 + pad)
        y2c = min(fh, y2 + pad)
        if x2c > x1c and y2c > y1c:
            img = img.crop((x1c, y1c, x2c, y2c))

    return img


def _fit_image(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    """Scale img down to fit within max_w × max_h, preserving aspect ratio.

    Never scales up — if the image is already smaller it is returned as-is.
    """
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    scale = min(max_w / w, max_h / h, 1.0)
    if scale < 1.0:
        return img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.LANCZOS,
        )
    return img


def _truncate_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: Any,
    max_w: int,
    ellipsis: str = "…",
) -> str:
    """Shorten text to fit within max_w pixels, appending an ellipsis."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_w:
            return text
        # Binary-search style: progressively strip from the right
        while len(text) > 1:
            text = text[:-1]
            cand = text.rstrip() + ellipsis
            bbox = draw.textbbox((0, 0), cand, font=font)
            if bbox[2] - bbox[0] <= max_w:
                return cand
        return text
    except Exception:
        return text[:25]


# ---------------------------------------------------------------------------
# Visual constants
# ---------------------------------------------------------------------------

_TILE_MAX_DIM  = 280   # longest side of a single tile (px); auto-adjusted for small N
_MARGIN        = 6     # gap between tiles (px)
_PADDING       = 14    # outer border on each side (px)
_CAPTION_H     = 20    # height reserved for caption text below each tile (px)
_CAPTION_FONT  = 13    # font size for captions
_BG_COLOR      = (18, 18, 18)    # canvas background
_TILE_BG       = (38, 38, 38)    # placeholder color for missing images
_CAPTION_COLOR = (195, 195, 195) # caption text color

# Font search: prefer bundled Hanken Grotesk, fall back to system fonts
# (paths and fallback chain live in generators._common)

# Clarendon OTF files — same paths used by the live scene-card renderer
_CLARENDON_DIR   = _FONTS_DIR / "libre_clarendon" / "fonts"
_CLARENDON_BOLD  = _CLARENDON_DIR / "LibreClarendonNormal-162Bold.otf"
_CLARENDON_LIGHT = _CLARENDON_DIR / "LibreClarendonNormal-42Light.otf"


def _load_font(size: int) -> Any:
    return load_font_with_fallback(size)


def _load_clarendon(bold: bool, size: int) -> Any:
    """Load Clarendon Bold or Light at *size* px, falling back to _load_font."""
    path = _CLARENDON_BOLD if bold else _CLARENDON_LIGHT
    return load_font_with_fallback(size, preferred_paths=[str(path)])


def make_intertitle_item(
    text: str,
    width: int = 320,
    height: int = 180,
    caption: str | None = None,
    *,
    is_title: bool = False,
    movie_year: str = "",
) -> MosaicItem:
    """Return a MosaicItem containing a grey intertitle image with centred white text.

    Replicates the live scene-card renderer:
    - Title card (is_title=True): bold title + light year, vertically centred.
    - Scene card (is_title=False): large light scene number, centred.

    Used for movie-title and scene-number cards when exporting to PDF.
    """
    img  = Image.new("RGB", (width, height), color=(120, 120, 120))
    draw = ImageDraw.Draw(img)
    WHITE = (255, 255, 255)

    def _text_size(fnt, s):
        """Return (w, h) for string *s* with font *fnt*."""
        try:
            bb = draw.textbbox((0, 0), s, font=fnt)
            return bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            return 0, 0

    def _draw_centred_x(fnt, s, y):
        tw, _ = _text_size(fnt, s)
        x = max(0, (width - tw) // 2)
        draw.text((x, y), s, font=fnt, fill=WHITE)

    if is_title:
        # Two-line block: title (Bold) + year (Light), vertically centred
        pt_title = max(1, round(height * 44 / 360))
        pt_year  = max(1, round(height * 28 / 360))
        gap      = max(2, round(height * 6  / 360))
        f_title  = _load_clarendon(bold=True,  size=pt_title)
        f_year   = _load_clarendon(bold=False, size=pt_year)
        _, lh_title = _text_size(f_title, text)
        _, lh_year  = _text_size(f_year,  movie_year) if movie_year else (0, 0)
        block_h = lh_title + (gap + lh_year if movie_year else 0)
        y0 = max(0, (height - block_h) // 2)
        _draw_centred_x(f_title, text, y0)
        if movie_year:
            _draw_centred_x(f_year, movie_year, y0 + lh_title + gap)
    else:
        # Scene-index card — large scene label in Light, centred
        pt_scene = max(1, round(height * 80 / 360))
        f_scene  = _load_clarendon(bold=False, size=pt_scene)
        tw, th   = _text_size(f_scene, text)
        x = max(0, (width  - tw) // 2)
        y = max(0, (height - th) // 2)
        draw.text((x, y), text, font=f_scene, fill=WHITE)

    return MosaicItem(pil_image=img, caption=caption or text)



def render_mosaic(
    items: list[MosaicItem],
    output_path: Path,
    layout: str = "landscape",
    tile_max_dim: int = _TILE_MAX_DIM,
    show_captions: bool = True,
    verbose: bool = True,
    progress_cb=None,
) -> Path:
    """Render a list of MosaicItems into a single grid image.

    Args:
        items:         MosaicItems to render (at least 1).
        output_path:   Destination file (.png or .jpg).
        layout:        "landscape" (wider grid) or "portrait" (taller grid).
        tile_max_dim:  Maximum pixel dimension (longest side) for one tile.
        show_captions: Draw caption text below each tile.
        progress_cb:   Optional callable(current: int, total: int) called after
                       each image is loaded during Pass 1.

    Returns:
        output_path after saving.

    Raises:
        ValueError: If items is empty or no images could be loaded.
    """
    # Ensure the JPEG codec is registered — it is loaded lazily by Pillow and
    # may be absent when saving a PDF if no JPEG has been opened in this process.
    import PIL.JpegImagePlugin  # noqa: F401

    if not items:
        raise ValueError("No items to render — list is empty.")

    cols, rows = _compute_grid(len(items), layout)

    # ------------------------------------------------------------------
    # Pass 1: load images and determine a uniform tile size.
    # Tile size is derived from the first successfully loaded image
    # so all tiles share the same bounding box.
    # ------------------------------------------------------------------
    loaded: list[Image.Image | None] = []
    tile_w: int | None = None
    tile_h: int | None = None

    if verbose:
        print(f"  Loading {len(items)} image(s)…", file=sys.stderr, flush=True)
    for item in items:
        img = _load_item_image(item)
        if img is not None and tile_w is None:
            w, h = img.size
            scale = tile_max_dim / max(w, h)
            tile_w = max(1, int(w * scale))
            tile_h = max(1, int(h * scale))
        loaded.append(img)
        if progress_cb is not None:
            progress_cb(len(loaded), len(items))

    if tile_w is None:
        raise ValueError("Could not load any images from the provided items.")

    # ------------------------------------------------------------------
    # Pass 2: compose canvas
    # ------------------------------------------------------------------
    cap_h = _CAPTION_H if show_captions else 0
    cell_w = tile_w
    cell_h = tile_h + cap_h

    canvas_w = _PADDING * 2 + cols * cell_w + (cols - 1) * _MARGIN
    canvas_h = _PADDING * 2 + rows * cell_h + (rows - 1) * _MARGIN

    canvas = Image.new("RGB", (canvas_w, canvas_h), color=_BG_COLOR)
    draw = ImageDraw.Draw(canvas)
    font = _load_font(_CAPTION_FONT) if show_captions else None

    for idx, (item, img) in enumerate(zip(items, loaded)):
        col = idx % cols
        row = idx // cols
        x0 = _PADDING + col * (cell_w + _MARGIN)
        y0 = _PADDING + row * (cell_h + _MARGIN)

        # Tile background (visible for missing images)
        draw.rectangle([x0, y0, x0 + cell_w - 1, y0 + tile_h - 1], fill=_TILE_BG)

        if img is not None:
            scaled = _fit_image(img, tile_w, tile_h)
            # Centre within the tile box
            x_img = x0 + (tile_w - scaled.width) // 2
            y_img = y0 + (tile_h - scaled.height) // 2
            canvas.paste(scaled, (x_img, y_img))

        if show_captions and item.caption and font is not None:
            cap_y = y0 + tile_h + 2
            text = _truncate_text(draw, item.caption, font, cell_w)
            draw.text((x0, cap_y), text, font=font, fill=_CAPTION_COLOR)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(output_path))
    return output_path


# ---------------------------------------------------------------------------
# Search-result consumer
# ---------------------------------------------------------------------------

def _find_video_path(project_path: str, movie_id: str) -> "Path | None":
    """Locate the video file whose stem matches *movie_id*.

    Searches both ``media/videos/movies/`` and ``media/videos/gameplay/``.
    Returns the first match or None.

    Example
    -------
        video = _find_video_path(project_path, "The Wild Bunch (1969) {tmdb-11232}")
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


def frame_from_pct(start_frame: int, end_frame: int, frame_pct: float) -> int:
    """Return the frame index at *frame_pct* through a shot.

    ``frame_pct`` is clamped to ``[0.0, 1.0]``.  ``0.0`` → start,
    ``0.5`` → middle, ``1.0`` → end.
    """
    pct = max(0.0, min(1.0, frame_pct))
    return int(start_frame + (end_frame - start_frame) * pct)


def mosaic_from_search_results(
    results: list[dict],
    project_path: str,
    *,
    output_path: "str | None" = None,
    layout: str = "landscape",
    show_captions: bool = True,
    frame_pct: float = 0.5,
    verbose: bool = True,
) -> Path:
    """Build a mosaic grid from ``search_shots()`` results.

    Each result becomes one tile in the grid.  The tile image is the video
    frame at *frame_pct* through the shot (0.0 = start, 0.5 = middle,
    1.0 = end).  Results whose video cannot be located on disk are silently
    skipped.

    Args:
        results:      List of result dicts from ``search_shots()["results"]``.
        project_path: Project root directory.
        output_path:  Destination file (auto-generated under
                      ``output/mosaics/`` if not provided).
        layout:       ``"landscape"`` (wider) or ``"portrait"`` (taller grid).
        show_captions: Draw ``movie_title`` below each tile.

    Returns:
        Path to the saved mosaic PNG.

    Raises:
        ValueError: If no frames could be resolved from the results.

    Example
    -------
        from services.search import search_shots
        from generators.mosaic import mosaic_from_search_results

        res = search_shots("gun", scopes=None, field=None,
                           limit=40, limit_per_item=None, use_all=True,
                           project_path=project_path)
        out = mosaic_from_search_results(res["results"], project_path)
    """
    import datetime

    items: list[MosaicItem] = []

    for r in results:
        movie_id = r.get("movie_id", "")
        video_path = _find_video_path(project_path, movie_id)
        if video_path is None:
            print(f"  ⚠ video not found for movie_id={movie_id!r} — skipping", file=sys.stderr, flush=True)
            continue

        sf = r.get("start_frame")
        ef = r.get("end_frame")
        if sf is not None and ef is not None:
            frame_index = frame_from_pct(int(sf), int(ef), frame_pct)
        elif sf is not None:
            frame_index = int(sf)
        else:
            frame_index = 0

        caption = r.get("movie_title") or movie_id

        items.append(MosaicItem(
            video_path=video_path,
            frame_index=int(frame_index),
            caption=caption,
            metadata=r,
        ))

    if not items:
        raise ValueError(
            "mosaic_from_search_results: no usable frames — "
            "check that video files are present for the matched movies."
        )

    if output_path:
        dest = Path(output_path)
    else:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        dest = (
            Path(project_path) / "output" / "mosaics"
            / f"search-mosaic-{stamp}.png"
        )

    return render_mosaic(items, dest, layout=layout, show_captions=show_captions, verbose=verbose)


# ---------------------------------------------------------------------------
# Shots / scenes data builders  (no Qt dependency — usable from CLI and GUI)
# ---------------------------------------------------------------------------

def build_shots_results(
    project_path: str,
    filename: str,
    *,
    best_mode: bool = False,
) -> "list[dict]":
    """Return tile result dicts for every shot in *filename*.

    Mirrors ``AllShotsWorker`` (best_mode=False) or ``BestOnlyWorker``
    (best_mode=True) without any Qt overhead, so the same data is available
    to both the CLI and the visualiser's PDF exporter.

    Each dict has at minimum: ``filename``, ``movie_id``, ``shot_id``,
    ``frame``, ``start_frame``, ``end_frame``, ``Ignore``, ``is_label=False``.
    """
    from data.shotlist import read_shotlist

    movie_id = Path(filename).stem
    shots    = read_shotlist(project_path, filename, "movie")

    if best_mode:
        from services.frame_match import load_best_frame_lookup
        lookup = load_best_frame_lookup(project_path, filename, "movie")
        return [
            {
                "filename":       filename,
                "movie_title":    filename,
                "movie_id":       movie_id,
                "shot_id":        shot_id,
                "frame":          bf.get("frame"),
                "start_frame":    bf.get("frame"),
                "end_frame":      None,
                "Ignore":         "",
                "is_label":       False,
                "matched_fields": [],
                "matched_text":   "",
                "score":          0.0,
            }
            for shot_id, bf in lookup.items()
        ]

    results: list[dict] = []
    for shot in shots:
        shot_id = shot.get("shot_id", "")
        sf      = shot.get("start_frame")
        try:
            frame_index = int(sf) if sf is not None else 0
        except (TypeError, ValueError):
            frame_index = 0
        results.append({
            "filename":       filename,
            "movie_title":    filename,
            "movie_id":       movie_id,
            "shot_id":        shot_id,
            "frame":          frame_index,
            "start_frame":    frame_index,
            "end_frame":      shot.get("end_frame"),
            "Ignore":         shot.get("Ignore", ""),
            "caption":        f"f{frame_index:06d}",
            "is_label":       False,
            "matched_fields": [],
            "matched_text":   "",
            "score":          0.0,
        })
    return results


def build_scenes_results(
    project_path: str,
    filename: str,
    *,
    best_mode: bool = False,
) -> "list[dict]":
    """Return tile result dicts for shots in *filename* grouped by scene.

    Mirrors ``ScenesWorker``: the list begins with a title card
    (``is_label=True, is_title=True``), then alternates scene-number cards
    (``is_label=True, is_title=False``) and frame tiles (``is_label=False``)
    whenever the ``Scene`` value changes.
    """
    import re as _re
    from data.shotlist import read_shotlist
    from data.metadata import get_metadata

    movie_id   = Path(filename).stem
    shots      = read_shotlist(project_path, filename, "movie")
    video_path = _find_video_path(project_path, movie_id)

    # Video dimensions for correctly-proportioned intertitle tiles
    vid_w, vid_h = 320, 180
    if video_path is not None:
        try:
            import cv2
            cap = cv2.VideoCapture(str(video_path))
            if cap.isOpened():
                _fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                _fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                if _fw > 0 and _fh > 0:
                    sar = _get_sar(str(video_path))
                    vid_w = int(round(_fw * sar[0] / sar[1]))
                    vid_h = _fh
            cap.release()
        except Exception:
            pass

    # Title and year from stored metadata, falling back to filename parsing
    meta_list = get_metadata(project_path, filename, "movie")
    if meta_list:
        _meta = meta_list[0]
    else:
        _stem  = Path(filename).stem
        _clean = _re.sub(r'\s*\{tmdb-\d+\}|\s*\(\d{4}\)', '', _stem).strip().rstrip('-').strip()
        _meta  = {
            "title": _clean.replace('-', ' ').replace('_', ' ').strip().title(),
            "year":  None,
        }
        _yr = _re.search(r'\((\d{4})\)', _stem)
        if _yr:
            _meta["year"] = int(_yr.group(1))

    display_title = str(_meta.get("title") or movie_id.replace("-", " ").replace("_", " "))
    movie_year    = str(_meta.get("year") or "")

    # Best-frame lookup (empty dict when best_mode=False)
    best_lookup: dict = {}
    if best_mode:
        from services.frame_match import load_best_frame_lookup
        best_lookup = load_best_frame_lookup(project_path, filename, "movie")

    results: list[dict] = []

    # Title card
    results.append({
        "filename":       filename,
        "movie_id":       movie_id,
        "is_label":       True,
        "is_title":       True,
        "label_text":     display_title,
        "movie_year":     movie_year,
        "vid_w":          vid_w,
        "vid_h":          vid_h,
        "caption":        "",
        "matched_fields": [],
        "matched_text":   "",
        "score":          0.0,
    })

    current_scene    = None
    scene_card_count = 0

    for shot in shots:
        scene   = str(shot.get("Scene") or "").strip()
        shot_id = shot.get("shot_id", "")
        sf      = shot.get("start_frame")
        try:
            frame_index = int(sf) if sf is not None else 0
        except (TypeError, ValueError):
            frame_index = 0

        # Scene-number card whenever the scene label changes
        if scene != current_scene:
            current_scene = scene
            results.append({
                "filename":       filename,
                "movie_id":       movie_id,
                "scene":          scene,
                "is_label":       True,
                "is_title":       False,
                "is_first_scene": scene_card_count == 0,
                "label_text":     scene or "?",
                "vid_w":          vid_w,
                "vid_h":          vid_h,
                "caption":        "",
                "matched_fields": [],
                "matched_text":   "",
                "score":          0.0,
            })
            scene_card_count += 1

        # Prefer precomputed best frame when available
        if best_lookup and shot_id in best_lookup:
            bf          = best_lookup[shot_id]
            frame_index = bf.get("frame", frame_index)

        results.append({
            "filename":       filename,
            "movie_title":    filename,
            "movie_id":       movie_id,
            "shot_id":        shot_id,
            "start_frame":    frame_index,
            "end_frame":      shot.get("end_frame"),
            "frame":          frame_index,
            "scene":          scene,
            "Ignore":         shot.get("Ignore", ""),
            "caption":        f"f{frame_index:06d}",
            "is_label":       False,
            "matched_fields": [],
            "matched_text":   "",
            "score":          0.0,
        })

    return results


def results_to_mosaic_items(
    results: "list[dict]",
    project_path: str,
) -> "list[MosaicItem]":
    """Convert tile result dicts to a ``MosaicItem`` list for ``render_mosaic``.

    Rules:
    - ``is_label=True``        → grey intertitle tile (via ``make_intertitle_item``)
    - ``is_label=False`` with ``Ignore`` truthy → skipped
    - ``is_label=False``       → video frame tile

    This is the single shared conversion used by both the CLI batch functions
    and ``PdfExportWorker`` in the visualiser, so the PDF output is identical
    regardless of how it was triggered.
    """
    # Reference dimensions for intertitle tiles — from the first label entry
    ref_vid_w, ref_vid_h = 320, 180
    for r in results:
        if r.get("is_label"):
            ref_vid_w = int(r.get("vid_w") or 320)
            ref_vid_h = int(r.get("vid_h") or 180)
            break

    items: list[MosaicItem] = []
    for r in results:
        if r.get("is_label"):
            label = r.get("label_text", "")
            items.append(make_intertitle_item(
                label, ref_vid_w, ref_vid_h,
                caption=label,
                is_title=bool(r.get("is_title")),
                movie_year=str(r.get("movie_year") or ""),
            ))
            continue

        # Skip ignored frame tiles
        if str(r.get("Ignore", "")).strip().lower() in ("true", "1", "yes"):
            continue

        movie_id   = r.get("movie_id", "")
        video_path = _find_video_path(project_path, movie_id)
        if video_path is None:
            continue

        frame = r.get("frame")
        if frame is None:
            sf = r.get("start_frame")
            ef = r.get("end_frame")
            if sf is not None and ef is not None:
                frame = int(sf + (ef - sf) * 0.5)
            elif sf is not None:
                frame = int(sf)
            else:
                frame = 0

        caption = r.get("caption") or r.get("movie_title", "") or movie_id
        items.append(MosaicItem(
            video_path=video_path,
            frame_index=int(frame),
            caption=caption,
        ))

    return items


def mosaic_pdf_from_shots(
    project_path: str,
    filename: str,
    *,
    best_mode: bool = False,
    output_path: "str | Path | None" = None,
    verbose: bool = True,
    progress_cb=None,
) -> Path:
    """Render a PDF contact sheet of all shots in *filename*.

    Equivalent to opening the Mosaic Visualiser, selecting a movie, pressing
    *Shots* (and optionally *Best*), then pressing *PDF*.

    Args:
        project_path: Project root directory.
        filename:     Movie filename (e.g. ``"3 10 To Yuma (1957) {tmdb-14168}.mkv"``).
        best_mode:    Use precomputed CLIP best-frame PNGs instead of the raw
                      first frame of each shot.
        output_path:  Override destination path (default: auto-named PDF under
                      ``output/mosaics/``).
        verbose:      Print progress to stderr.
        progress_cb:  Optional ``callable(current, total)`` called after each
                      image is loaded (for live progress reporting).

    Returns:
        Path to the saved PDF.
    """
    import datetime
    import re as _re

    results = build_shots_results(project_path, filename, best_mode=best_mode)
    items   = results_to_mosaic_items(results, project_path)
    if not items:
        raise ValueError(f"mosaic_pdf_from_shots: no usable frames for {filename!r}")

    if output_path:
        dest = Path(output_path)
    else:
        stem  = _re.sub(r'[/\\:*?"<>|]', '_', Path(filename).stem)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        dest  = Path(project_path) / "output" / "mosaics" / "shots" / f"{stem} [{stamp}].pdf"

    return render_mosaic(items, dest, layout="landscape",
                         verbose=verbose, progress_cb=progress_cb)


def mosaic_pdf_from_scenes(
    project_path: str,
    filename: str,
    *,
    best_mode: bool = False,
    output_path: "str | Path | None" = None,
    verbose: bool = True,
    progress_cb=None,
) -> Path:
    """Render a PDF contact sheet of shots in *filename* grouped by scene.

    Equivalent to opening the Mosaic Visualiser, selecting a movie, pressing
    *Scenes* (and optionally *Best*), then pressing *PDF*.  Title cards and
    scene-number intertitles are included, and ignored shots are skipped.

    Args:
        project_path: Project root directory.
        filename:     Movie filename.
        best_mode:    Use precomputed CLIP best-frame PNGs.
        output_path:  Override destination path.
        verbose:      Print progress to stderr.
        progress_cb:  Optional ``callable(current, total)``.

    Returns:
        Path to the saved PDF.
    """
    import datetime
    import re as _re

    results = build_scenes_results(project_path, filename, best_mode=best_mode)
    items   = results_to_mosaic_items(results, project_path)
    if not items:
        raise ValueError(f"mosaic_pdf_from_scenes: no usable frames for {filename!r}")

    if output_path:
        dest = Path(output_path)
    else:
        stem  = _re.sub(r'[/\\:*?"<>|]', '_', Path(filename).stem)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        dest  = Path(project_path) / "output" / "mosaics" / "scenes" / f"{stem} [{stamp}].pdf"

    return render_mosaic(items, dest, layout="landscape",
                         verbose=verbose, progress_cb=progress_cb)


# ---------------------------------------------------------------------------
# Per-frame JPEG exporter
# ---------------------------------------------------------------------------

def _extract_frames_for_tile(
    video_path: Path, frame_indices: "list[int]"
) -> "list[Image.Image | None]":
    """Extract multiple frames from a single video in one open/close cycle.

    More efficient than calling ``extract_frame_pil`` N times because the
    VideoCapture is opened only once.  Frames are sought in the order given
    by *frame_indices*; each element of the returned list corresponds to the
    same-index element of *frame_indices* and is ``None`` on failure.
    """
    try:
        import cv2
        import numpy as np  # noqa: F401 – needed by cv2.cvtColor

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return [None] * len(frame_indices)

        sar = _get_sar(str(video_path))
        out: list[Image.Image | None] = []
        for fi in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ret, bgr = cap.read()
            if ret:
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                if sar != (1, 1):
                    display_w = int(round(rgb.shape[1] * sar[0] / sar[1]))
                    rgb = cv2.resize(rgb, (display_w, rgb.shape[0]), interpolation=cv2.INTER_LINEAR)
                out.append(Image.fromarray(rgb))
            else:
                out.append(None)
        cap.release()
        return out
    except Exception:
        return [None] * len(frame_indices)


# ---------------------------------------------------------------------------
# Video mosaic
# ---------------------------------------------------------------------------

def mosaic_video_from_search_results(
    results: "list[dict]",
    project_path: str,
    *,
    layout: str = "landscape",
    fps: int = 8,
    duration: int = 2,
    limit: int = 50,
    query: "str | None" = None,
    verbose: bool = True,
) -> Path:
    """Render a looping video mosaic from ``search_shots()`` results.

    Each tile in the grid shows a short temporal loop around the best/midpoint
    frame of the matched shot.  All tiles are synchronised – every frame of the
    output video advances all tiles by one time step simultaneously.

    Args:
        results:      List of result dicts from ``search_shots()["results"]``.
        project_path: Project root directory.
        layout:       ``"landscape"`` (wider) or ``"portrait"`` (taller grid).
        fps:          Output frame rate (default 8).
        duration:     Loop length in seconds (default 2).
        limit:        Maximum number of tiles (default 50).
        query:        Optional query string used in the output filename.

    Returns:
        Path to the saved ``.mp4`` file.

    Raises:
        ValueError: If no frames could be extracted from any result.
    """
    import datetime
    import re

    import numpy as np

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        raise ImportError("opencv-python is required for video mosaic generation") from exc

    results = results[:limit]
    if not results:
        raise ValueError("mosaic_video_from_search_results: results list is empty")

    num_frames = fps * duration
    offsets = np.linspace(-0.5, 0.5, num_frames)

    cols, rows_count = _compute_grid(len(results), layout)

    # ------------------------------------------------------------------
    # Pass 1: collect frame indices per tile and extract them in batches
    # ------------------------------------------------------------------
    tile_frame_arrs: list[list["np.ndarray | None"]] = []
    tile_w: int | None = None
    tile_h: int | None = None

    for r in results:
        movie_id   = r.get("movie_id", "")
        video_path = _find_video_path(project_path, movie_id)

        sf = r.get("start_frame")
        ef = r.get("end_frame")
        sf = int(sf) if sf is not None else 0
        ef = int(ef) if ef is not None else sf + 1
        shot_len = max(1, ef - sf)

        if r.get("best_frame") is not None:
            center = int(r["best_frame"])
        else:
            center = (sf + ef) // 2

        frame_indices = [
            max(sf, min(ef, int(round(center + o * shot_len))))
            for o in offsets
        ]

        if video_path is not None:
            pil_frames = _extract_frames_for_tile(video_path, frame_indices)
        else:
            print(f"  ⚠ video not found for movie_id={movie_id!r} — skipping tile", file=sys.stderr, flush=True)
            pil_frames = [None] * num_frames

        # Convert PIL → numpy (RGB); determine tile size from first valid frame
        arr_frames: list[np.ndarray | None] = []
        for pil in pil_frames:
            if pil is None:
                arr_frames.append(None)
                continue
            arr = np.asarray(pil.convert("RGB"))
            if tile_w is None:
                h, w = arr.shape[:2]
                scale = _TILE_MAX_DIM / max(w, h)
                tile_w = max(1, int(w * scale))
                tile_h = max(1, int(h * scale))
            arr_frames.append(arr)
        tile_frame_arrs.append(arr_frames)

    if tile_w is None:
        raise ValueError(
            "mosaic_video_from_search_results: no frames could be extracted — "
            "check that video files are present for the matched movies."
        )

    # ------------------------------------------------------------------
    # Pass 2: resize all frames to a uniform tile size; fill placeholders
    # ------------------------------------------------------------------
    placeholder = np.full((tile_h, tile_w, 3), _TILE_BG, dtype=np.uint8)

    for tile_list in tile_frame_arrs:
        for t, arr in enumerate(tile_list):
            if arr is None:
                tile_list[t] = placeholder
            elif arr.shape[:2] != (tile_h, tile_w):
                pil_r = Image.fromarray(arr).resize((tile_w, tile_h), Image.LANCZOS)
                tile_list[t] = np.asarray(pil_r)

    # ------------------------------------------------------------------
    # Pass 3: compute canvas dimensions
    # ------------------------------------------------------------------
    canvas_w = _PADDING * 2 + cols * tile_w + (cols - 1) * _MARGIN
    canvas_h = _PADDING * 2 + rows_count * tile_h + (rows_count - 1) * _MARGIN

    # ------------------------------------------------------------------
    # Pass 4: output path
    # ------------------------------------------------------------------
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    if query:
        safe_q = re.sub(r"[^\w\-]", "_", query)[:40].strip("_")
        filename = f"{safe_q}-mosaic-video-{stamp}.mp4"
    else:
        filename = f"mosaic-video-{stamp}.mp4"

    output_path = (
        Path(project_path) / "output" / "mosaics" / "video" / "search" / filename
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Pass 5: encode video
    # ------------------------------------------------------------------
    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        float(fps),
        (canvas_w, canvas_h),
    )

    bg_color_bgr = (_BG_COLOR[2], _BG_COLOR[1], _BG_COLOR[0])
    n_tiles = len(tile_frame_arrs)

    for t in range(num_frames):
        canvas = np.full((canvas_h, canvas_w, 3), bg_color_bgr, dtype=np.uint8)

        for idx in range(n_tiles):
            col = idx % cols
            row = idx // cols
            x0 = _PADDING + col * (tile_w + _MARGIN)
            y0 = _PADDING + row * (tile_h + _MARGIN)

            arr = tile_frame_arrs[idx][t]
            # Convert RGB → BGR for OpenCV
            bgr_tile = arr[:, :, ::-1]
            canvas[y0:y0 + tile_h, x0:x0 + tile_w] = bgr_tile

        writer.write(canvas)

    writer.release()
    return output_path


# ---------------------------------------------------------------------------
# Per-frame JPEG exporter
# ---------------------------------------------------------------------------

def export_frames_from_search_results(
    results: list[dict],
    project_path: str,
    query: str,
    *,
    field: "str | None" = None,
    frame_pct: float = 0.5,
    jpeg_quality: int = 92,
    verbose: bool = True,
) -> Path:
    """Export each search result as an individual JPEG with an info overlay.

    Files are written to::

        <project>/output/exports/<stem>-<YYYY-MM-DD-HH-MM-SS>/

    where *stem* is the sanitised query string.  Each file is named::

        <zero-padded-index>-<movie_id>-shot<shot_id>.jpg

    An info bar is burned into the bottom of every frame showing:
      • the query string (and field, if filtered)
      • the result index  (e.g. ``#001 / 042``)
      • the movie title and shot id

    Args:
        results:      List of result dicts from ``search_shots()["results"]``.
        project_path: Project root directory.
        query:        The search string used to produce *results*.
        field:        Annotation field filter (``None`` = all fields).
        frame_pct:    Frame position within the shot (0.0–1.0).
        jpeg_quality: JPEG quality 1–95 (default 92).

    Returns:
        Path to the export folder.

    Raises:
        ValueError: If no frames could be exported.
    """
    import datetime
    import re

    if not results:
        raise ValueError("export_frames_from_search_results: results list is empty")

    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    safe_query = re.sub(r"[^\w\-]", "_", query)[:40].strip("_")
    folder_name = f"{safe_query}-{stamp}"
    export_dir = Path(project_path) / "output" / "exports" / folder_name
    export_dir.mkdir(parents=True, exist_ok=True)

    # Info bar visual constants
    BAR_H        = 36
    BAR_BG       = (15, 15, 15)
    TEXT_COLOR   = (220, 220, 220)
    ACCENT_COLOR = (255, 200, 80)
    FONT_SIZE    = 14
    FONT_SIZE_SM = 12

    font_main = _load_font(FONT_SIZE)
    font_sm   = _load_font(FONT_SIZE_SM)

    total = len(results)
    exported = 0
    skipped  = 0

    for idx, r in enumerate(results):
        movie_id   = r.get("movie_id", "")
        video_path = _find_video_path(project_path, movie_id)
        if video_path is None:
            print(f"  ⚠ [{idx+1}/{total}] video not found for {movie_id!r} — skipping", file=sys.stderr, flush=True)
            skipped += 1
            continue

        sf = r.get("start_frame")
        ef = r.get("end_frame")
        if sf is not None and ef is not None:
            frame_index = frame_from_pct(int(sf), int(ef), frame_pct)
        elif sf is not None:
            frame_index = int(sf)
        else:
            frame_index = 0

        img = extract_frame_pil(video_path, frame_index)
        if img is None:
            print(f"  ⚠ [{idx+1}/{total}] frame extraction failed — skipping", file=sys.stderr, flush=True)
            skipped += 1
            continue

        # Build info bar
        bar = Image.new("RGB", (img.width, BAR_H), color=BAR_BG)
        draw = ImageDraw.Draw(bar)

        # Left: query + field
        field_suffix = f"  [{field}]" if field else ""
        left_text = f"query: {query}{field_suffix}"
        draw.text((8, 4), left_text, font=font_sm, fill=TEXT_COLOR)

        # Right: index counter
        index_text = f"#{idx+1:03d} / {total:03d}"
        if font_main:
            bbox = draw.textbbox((0, 0), index_text, font=font_main)
            ix = img.width - (bbox[2] - bbox[0]) - 10
        else:
            ix = img.width - 90
        draw.text((ix, 3), index_text, font=font_main, fill=ACCENT_COLOR)

        # Second line: movie title + shot id
        movie_title = r.get("movie_title") or movie_id
        shot_id     = r.get("shot_id", "")
        detail_text = f"{movie_title}  •  shot {shot_id}"
        draw.text((8, BAR_H // 2 + 2), detail_text, font=font_sm, fill=TEXT_COLOR)

        # Append bar below frame
        composite = Image.new("RGB", (img.width, img.height + BAR_H))
        composite.paste(img, (0, 0))
        composite.paste(bar, (0, img.height))

        # Filename
        safe_mid  = re.sub(r"[^\w\-]", "_", movie_id)[:50]
        filename  = f"{idx+1:04d}-{safe_mid}-shot{shot_id}.jpg"
        dest      = export_dir / filename
        composite.save(str(dest), "JPEG", quality=jpeg_quality, optimize=True)
        exported += 1

    if exported == 0:
        raise ValueError(
            "export_frames_from_search_results: no frames could be exported — "
            "check that video files exist for the matched movies."
        )

    if verbose:
        print(f"  ✓ Exported {exported} frame(s) ({skipped} skipped) → {export_dir}", file=sys.stderr, flush=True)
    return export_dir

