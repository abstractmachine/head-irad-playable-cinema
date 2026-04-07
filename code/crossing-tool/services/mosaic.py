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
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MosaicItem:
    """A single cell in the mosaic grid.

    Either image_path or (video_path + frame_index) must be set.
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
    caption: str | None = None
    metadata: dict[str, Any] | None = None
    crop_bbox: list[int] | None = None   # [x1, y1, x2, y2]
    crop_padding: int = 20               # context pixels around the crop


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

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

# Font search: prefer a clean system font, fall back gracefully
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]


def _load_font(size: int) -> Any:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def render_mosaic(
    items: list[MosaicItem],
    output_path: Path,
    layout: str = "landscape",
    tile_max_dim: int = _TILE_MAX_DIM,
    show_captions: bool = True,
) -> Path:
    """Render a list of MosaicItems into a single grid image.

    Args:
        items:         MosaicItems to render (at least 1).
        output_path:   Destination file (.png or .jpg).
        layout:        "landscape" (wider grid) or "portrait" (taller grid).
        tile_max_dim:  Maximum pixel dimension (longest side) for one tile.
        show_captions: Draw caption text below each tile.

    Returns:
        output_path after saving.

    Raises:
        ValueError: If items is empty or no images could be loaded.
    """
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

    print(f"  Loading {len(items)} image(s)…", flush=True)
    for item in items:
        img = _load_item_image(item)
        if img is not None and tile_w is None:
            w, h = img.size
            scale = tile_max_dim / max(w, h)
            tile_w = max(1, int(w * scale))
            tile_h = max(1, int(h * scale))
        loaded.append(img)

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
