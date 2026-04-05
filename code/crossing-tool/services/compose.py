"""compose — experimental generative poster / canvas tool.

Takes text events (with quad geometry) from one or more films, extracts the
text patches from their source video frames — optionally using SAM for
precise alpha masking — and composites them onto a randomly sampled
background frame to produce a poster or landscape canvas.

Output: JPEG or PDF, saved to <project>/media/compositions/ and optionally opened
in the Ubuntu desktop viewer (xdg-open).

SAM integration (optional):
    If ``ultralytics`` is installed the tool will use SAM 2 with a bounding-
    box prompt to segment the text pixels precisely, giving each patch a
    clean alpha channel that isolates only the letters/words.

    If SAM is unavailable the tool falls back to an Otsu luminance threshold
    which works well for high-contrast title cards (white text on black).
    A feathered polygon mask based on the quad is applied as a final clip so
    the patch edges are always tight.

Canvas presets:
    portrait  — 1240 × 1754 px  (≈ A3 @ 150 dpi)
    landscape — 1920 × 1080 px  (Full HD)
"""

from __future__ import annotations

import json
import math
import random
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageFilter

# ---------------------------------------------------------------------------
# Canvas presets
# ---------------------------------------------------------------------------

CANVAS_SIZES: dict[str, tuple[int, int]] = {
    "portrait":  (1240, 1754),
    "landscape": (1920, 1080),
}

# ---------------------------------------------------------------------------
# SAM — lazy singleton
# ---------------------------------------------------------------------------

_sam_model = None
_sam_checked: bool = False
_sam_model_path: Path | None = None


def _get_sam_model(project_path: str) -> Any:
    """Return the shared SAM2 model, loading it on first call.

    Looks for the model at ``<project_path>/models/sam2.1_b.pt``.

    Raises:
        RuntimeError: If ``ultralytics`` is not installed or model file is missing.
    """
    global _sam_model, _sam_checked, _sam_model_path
    if _sam_checked:
        return _sam_model
    _sam_checked = True
    _sam_model_path = Path(project_path) / "models" / "sam2.1_b.pt"
    if not _sam_model_path.exists():
        raise RuntimeError(
            f"SAM model not found at {_sam_model_path}. "
            "Place sam2.1_b.pt in <project>/models/."
        )
    try:
        from ultralytics import SAM as _SAM  # type: ignore
        _sam_model = _SAM(str(_sam_model_path))
    except ImportError:
        raise RuntimeError(
            "SAM requires the 'ultralytics' package.  "
            "Install it with: pip install ultralytics"
        )
    return _sam_model


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


def _pick_bg_frame(
    video_path: str,
    rng: random.Random,
    exclude_ends: float = 0.05,
) -> "np.ndarray | None":
    """Pick a random non-blank RGB frame (SAR-corrected)."""
    total = _frame_count(video_path)
    if total <= 0:
        return None
    lo = int(total * exclude_ends)
    hi = int(total * (1.0 - exclude_ends))
    if hi <= lo:
        lo, hi = 0, total
    sar = _get_sar(video_path)
    for _ in range(30):
        fn = rng.randint(lo, hi - 1)
        bgr = _extract_frame(video_path, fn)
        if bgr is None:
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        rgb = _sar_correct(rgb, *sar)
        if np.mean(rgb) > 12:   # skip near-black frames
            return rgb
    return None


# ---------------------------------------------------------------------------
# Patch extraction helpers
# ---------------------------------------------------------------------------

def _quad_aabb(quad: list[int]) -> tuple[int, int, int, int]:
    xs, ys = quad[0::2], quad[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def _pad_bbox(
    x1: int, y1: int, x2: int, y2: int,
    factor: float, w: int, h: int,
) -> tuple[int, int, int, int]:
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    hw = (x2 - x1) * factor / 2
    hh = (y2 - y1) * factor / 2
    return (
        max(0, int(cx - hw)), max(0, int(cy - hh)),
        min(w, int(cx + hw)), min(h, int(cy + hh)),
    )


def _luminance_mask(patch_rgb: "np.ndarray") -> "np.ndarray":
    """Otsu threshold producing a uint8 mask (255 = text, 0 = background).

    Automatically detects whether the text is light-on-dark or dark-on-light
    by comparing mean luminance to 128.
    """
    grey = cv2.cvtColor(patch_rgb, cv2.COLOR_RGB2GRAY)
    grey = cv2.GaussianBlur(grey, (3, 3), 0)
    _, mask = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # If mean luminance > 128 the background is bright, so text is dark → invert
    if np.mean(grey) > 128:
        mask = 255 - mask
    # Small dilation to recover anti-aliased edges
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=1)
    return mask


def _refine_mask_with_luminance(
    patch_rgb: "np.ndarray",
    sam_mask: "np.ndarray",
) -> "np.ndarray":
    """Tighten a SAM blob mask using per-pixel luminance gating.

    SAM returns a filled region covering the whole text card; this step
    zeros-out background pixels that bleed through the gaps between and
    inside letter forms, while preserving the smooth feathered edge that
    SAM produces at the region boundary.
    """
    grey = cv2.cvtColor(patch_rgb, cv2.COLOR_RGB2GRAY)

    # Boost local contrast so text edges are sharp even in low-contrast frames
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    grey_eq = clahe.apply(grey)

    # Otsu threshold on the CLAHE-enhanced grey
    _, lum = cv2.threshold(grey_eq, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Auto-invert for dark text on bright background
    if float(np.mean(grey)) > 128:
        lum = 255 - lum

    # Morphological cleanup: close small holes inside letter forms,
    # then open to remove isolated noise dots
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    lum = cv2.morphologyEx(lum, cv2.MORPH_CLOSE, kernel, iterations=2)
    lum = cv2.morphologyEx(lum, cv2.MORPH_OPEN,  kernel, iterations=1)

    # Feathered SAM edge × binary luminance gate:
    # text pixels inside the SAM region inherit the smooth SAM boundary;
    # background bleed-through is zeroed out.
    sam_soft = cv2.GaussianBlur(sam_mask, (5, 5), 2)
    combined = np.minimum(sam_soft.astype(np.float32), lum.astype(np.float32))
    return combined.astype(np.uint8)


def _polygon_mask(shape_hw: tuple[int, int], pts: "np.ndarray") -> "np.ndarray":
    """Soft filled polygon mask clipped to shape."""
    h, w = shape_hw
    m = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(m, [pts.reshape(-1, 1, 2).astype(np.int32)], 255)
    return cv2.GaussianBlur(m, (9, 9), 4)


def _sam_mask(
    frame_rgb: "np.ndarray",
    bbox: tuple[int, int, int, int],
    sam_model,
) -> "np.ndarray | None":
    """Run SAM on the full frame with a bounding-box prompt.

    Returns a uint8 mask (0/255) the same H×W as frame_rgb, or None.
    """
    try:
        x1, y1, x2, y2 = bbox
        results = sam_model(frame_rgb, bboxes=[[x1, y1, x2, y2]], verbose=False)
        if not results or results[0].masks is None:
            return None
        mask_t = results[0].masks.data[0]
        mask = mask_t.cpu().numpy().astype(np.uint8) * 255
        fh, fw = frame_rgb.shape[:2]
        if mask.shape != (fh, fw):
            mask = cv2.resize(mask, (fw, fh), interpolation=cv2.INTER_NEAREST)
        return mask
    except Exception:
        return None


def _extract_patch(
    frame_rgb: "np.ndarray",
    quad: list[int],
    *,
    sam_model,
) -> "Image.Image | None":
    """Extract a text region as an RGBA PIL Image.

    Pipeline:
    1. SAM: run on the full frame with the quad's AABB as bbox prompt, crop
       the result to the padded bbox window.  This is the primary path.
    2. Luminance threshold fallback: used only when SAM returns no mask for
       a specific patch (e.g. very small bbox or degenerate image region).
    3. Last resort: feathered polygon mask only.
    """
    if not quad or len(quad) != 8:
        return None
    h, w = frame_rgb.shape[:2]
    x1, y1, x2, y2 = _quad_aabb(quad)
    if x2 <= x1 or y2 <= y1:
        return None

    # --- 1. SAM path ---------------------------------------------------------
    bx1, by1, bx2, by2 = _pad_bbox(x1, y1, x2, y2, 1.25, w, h)
    full_mask = _sam_mask(frame_rgb, (bx1, by1, bx2, by2), sam_model)
    if full_mask is not None:
        patch_rgb  = frame_rgb[by1:by2, bx1:bx2]
        patch_mask = full_mask[by1:by2, bx1:bx2]
        patch_mask = _refine_mask_with_luminance(patch_rgb, patch_mask)
        white = np.full_like(patch_rgb, 255)
        rgba = np.dstack([white, patch_mask])
        return Image.fromarray(rgba, "RGBA")

    # --- 2. Luminance threshold + polygon clip path --------------------------
    bx1, by1, bx2, by2 = _pad_bbox(x1, y1, x2, y2, 1.15, w, h)
    patch_rgb = frame_rgb[by1:by2, bx1:bx2].copy()
    if patch_rgb.size == 0:
        return None

    lum_mask = _luminance_mask(patch_rgb)

    # Translate quad to crop-local coordinate space
    local_pts = np.array(
        [(quad[i * 2] - bx1, quad[i * 2 + 1] - by1) for i in range(4)],
        dtype=np.float32,
    )
    poly_mask = _polygon_mask(patch_rgb.shape[:2], local_pts)

    # Combine: must pass both luminance gate AND lie within the polygon
    combined = cv2.bitwise_and(lum_mask, poly_mask)
    combined = cv2.GaussianBlur(combined, (5, 5), 2)

    # 3. Fallback: if combined mask is almost empty, use polygon only
    if combined.max() < 10:
        combined = poly_mask

    white = np.full_like(patch_rgb, 255)
    rgba = np.dstack([white, combined])
    return Image.fromarray(rgba, "RGBA")


# ---------------------------------------------------------------------------
# Background treatment
# ---------------------------------------------------------------------------

_BG_TREATMENTS = ("hue_tint",)


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
# Element placement
# ---------------------------------------------------------------------------

def _place_patch(
    canvas: Image.Image,
    patch: Image.Image,
    rng: random.Random,
    canvas_w: int,
    canvas_h: int,
    *,
    scale: float | None = None,
    angle: float | None = None,
    opacity: float | None = None,
) -> None:
    """Scale, rotate, set opacity, and paste a patch at a random position."""
    sc = scale   if scale   is not None else rng.uniform(0.4, 2.2)
    ag = angle   if angle   is not None else rng.uniform(-45.0, 45.0)
    op = opacity if opacity is not None else rng.uniform(0.35, 1.0)

    nw = max(8, int(patch.width  * sc))
    nh = max(8, int(patch.height * sc))
    patch = patch.resize((nw, nh), Image.LANCZOS)
    patch = patch.rotate(ag, expand=True, resample=Image.BICUBIC)

    if op < 1.0:
        r, g, b, a = patch.split()
        a = a.point(lambda x: int(x * op))
        patch = Image.merge("RGBA", (r, g, b, a))

    # Allow up to 25 % bleed beyond each edge for a full-bleed look
    cx = rng.randint(-patch.width  // 4, canvas_w + patch.width  // 4)
    cy = rng.randint(-patch.height // 4, canvas_h + patch.height // 4)
    canvas.paste(patch, (cx - patch.width // 2, cy - patch.height // 2), patch)


# ---------------------------------------------------------------------------
# Main compose function
# ---------------------------------------------------------------------------

def compose(
    filenames: list[str],
    project_path: str,
    *,
    media_type: str = "movies",
    orientation: str = "portrait",
    width: int | None = None,
    height: int | None = None,
    n_elements: int | None = None,
    bg_frame: int | None = None,
    bg_treatment: str | None = None,
    seed: int | None = None,
    output_path: str | None = None,
    fmt: str = "jpg",
    open_result: bool = True,
    verbose: bool = False,
) -> Path:
    """Run the composition pipeline and return the saved output path.

    SAM 2 is required for alpha-masked patch extraction.  Install ultralytics
    before running: ``pip install ultralytics``.

    Args:
        filenames:    Video filenames to draw text events from.
        project_path: Project root.
        media_type:   ``"movies"`` or ``"gameplay"``.
        orientation:  ``"portrait"`` or ``"landscape"`` (sets default canvas).
        width/height: Override canvas dimensions.
        n_elements:   Number of text patches to composite (random 6–18 if None).
        bg_frame:     Specific frame number to use as background (random if None).
        bg_treatment: One of ``desaturate``, ``tint``, ``darken``, ``original``
                      (random if None).
        seed:         RNG seed for reproducibility (random if None).
        output_path:  Save path override (auto-generated if None).
        fmt:          ``"jpg"`` or ``"pdf"``.
        open_result:  Open the saved file with ``xdg-open``.
        verbose:      Print progress.

    Returns:
        :class:`pathlib.Path` to the saved file.
    """
    from services.text_extraction import get_text_csv_path, read_text_csv

    rng = random.Random(seed)

    # --- Canvas ------------------------------------------------------------------
    default_w, default_h = CANVAS_SIZES.get(orientation, CANVAS_SIZES["portrait"])
    canvas_w = width  or default_w
    canvas_h = height or default_h
    if verbose:
        print(f"  Canvas: {canvas_w}×{canvas_h}  orientation={orientation}  seed={seed}")

    # --- Collect text events that have quad data ---------------------------------
    all_events: list[dict[str, Any]] = []
    for fn in filenames:
        csv_path = get_text_csv_path(project_path, fn, media_type)
        if not csv_path.exists():
            if verbose:
                print(f"  ⚠ No text CSV for {fn} — skipping")
            continue
        for row in read_text_csv(project_path, fn, media_type):
            q = row.get("quad", "")
            if q and len(q.split(",")) == 8:
                try:
                    int(row["start_frame"])
                except (KeyError, ValueError, TypeError):
                    continue
                row["_filename"] = fn
                all_events.append(row)

    if not all_events:
        raise RuntimeError(
            "No text events with quad data found. "
            "Run `crossing text detect` first (quads require PaddleOCR 3.x)."
        )
    if verbose:
        print(f"  {len(all_events)} quad events from {len(filenames)} film(s)")

    # --- Background frame --------------------------------------------------------
    bg_filename = rng.choice(filenames)
    bg_video    = Path(project_path) / "media" / "videos" / media_type / bg_filename
    if not bg_video.exists():
        raise FileNotFoundError(f"Video not found: {bg_video}")

    if bg_frame is not None:
        bgr = _extract_frame(str(bg_video), bg_frame)
        if bgr is None:
            raise RuntimeError(f"Could not extract frame {bg_frame} from {bg_video}")
        bg_rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        bg_rgb = _sar_correct(bg_rgb, *_get_sar(str(bg_video)))
    else:
        bg_rgb = _pick_bg_frame(str(bg_video), rng)
        if bg_rgb is None:
            raise RuntimeError(f"Could not extract a usable background frame from {bg_video}")

    if verbose:
        print(f"  Background: {bg_filename}")

    canvas = _apply_bg_treatment(Image.fromarray(bg_rgb), canvas_w, canvas_h, rng, bg_treatment)

    # --- SAM model ---------------------------------------------------------------
    sam_model = _get_sam_model(project_path)  # raises RuntimeError if ultralytics not installed
    if verbose:
        print("  SAM: ✓ loaded")

    # --- Select and place elements -----------------------------------------------
    n = n_elements if n_elements is not None else rng.randint(6, 18)
    selected = rng.choices(all_events, k=n) if len(all_events) < n else rng.sample(all_events, n)

    sar_cache: dict[str, tuple[int, int]] = {}
    placed = 0

    for evt in selected:
        fn = evt["_filename"]
        video_path = Path(project_path) / "media" / "videos" / media_type / fn
        if not video_path.exists():
            continue

        frame_no = int(evt["start_frame"])
        try:
            quad = [int(v) for v in evt["quad"].split(",")]
        except (ValueError, AttributeError):
            continue

        if fn not in sar_cache:
            sar_cache[fn] = _get_sar(str(video_path))
        sar_num, sar_den = sar_cache[fn]

        bgr = _extract_frame(str(video_path), frame_no)
        if bgr is None:
            continue

        frame_rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        frame_rgb = _sar_correct(frame_rgb, sar_num, sar_den)

        # SAR-correct the quad X coordinates
        if (sar_num, sar_den) != (1, 1):
            quad = [
                int(round(v * sar_num / sar_den)) if i % 2 == 0 else v
                for i, v in enumerate(quad)
            ]

        patch = _extract_patch(frame_rgb, quad, sam_model=sam_model)
        if patch is None:
            continue

        _place_patch(canvas, patch, rng, canvas_w, canvas_h)
        placed += 1

        if verbose:
            snippet = evt.get("text", "")[:50]
            print(f"    [{placed}/{n}] {fn}: {snippet!r}")

    if verbose:
        print(f"  Placed {placed}/{n} patches")

    # --- Save ---------------------------------------------------------------
    out_dir = Path(project_path) / "media" / "compositions"
    out_dir.mkdir(parents=True, exist_ok=True)

    if output_path:
        out_path = Path(output_path)
    else:
        import datetime
        stamp    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        seed_str = str(seed) if seed is not None else "rnd"
        stem     = (
            Path(filenames[0]).stem[:30]
            if len(filenames) == 1
            else f"{len(filenames)}films"
        )
        out_path = out_dir / f"composition_{stem}_{stamp}_{seed_str}.{fmt}"

    final = canvas.convert("RGB") if fmt in ("jpg", "jpeg") else canvas
    if fmt == "pdf":
        final.save(str(out_path), "PDF", resolution=150)
    else:
        final.save(str(out_path), "JPEG", quality=93)

    if open_result:
        try:
            subprocess.Popen(["xdg-open", str(out_path)])
        except Exception:
            pass

    return out_path
