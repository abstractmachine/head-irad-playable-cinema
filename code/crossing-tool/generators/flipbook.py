"""Flipbook generator — cinematic semantic reduction of shot annotations.

Builds a one-page-per-shot book where:
- the background is the shot's palette background color
- the foreground is a single large motif word in the shot's palette foreground color
- typography uses Libre Clarendon Normal Medium (LibreClarendonNormal-110Medium.otf)
- page ratio is 16:9 (cinema widescreen)
- front cover shows the movie title; back cover shows the year

This is not a storyboard and not subtitles.
It is a semantic cinematic reduction: a sequence of color-and-word spreads
that let a reader feel the progression, rhythm, and symbolic drift of the western.

Data flow
---------
The flipbook is a renderer, not a semantic generator.  It reads:
  - motif values from ``entry["shot"]["motif"]["value"]``
  - figure palette from ``data/palettes/<media_type>/<stem>.json``
    via fields ``shot["foreground"]["rgb"]`` / ``shot["background"]["rgb"]``

It does NOT:
  - decide the motif
  - decide the palette
  - reinterpret the shot semantically

Output
------
PDF: ``<project>/output/flipbooks/<stem>-flipbook.pdf``

Page schema (internal, used by visualizer and renderer):
  {
    "kind":        "shot" | "cover_front" | "cover_back",
    "text":        str,          # motif word, title, or year
    "bg_rgb":      [R, G, B],
    "fg_rgb":      [R, G, B],
    "shot_id":     str | None,
    "shot_index":  int | None,
    "start_time":  str,
    "motif":       str,          # copy of text for shot pages
  }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canvas dimensions (logical pixels — suitable for PDF at 150 dpi)
PAGE_W = 1920
PAGE_H = 1080

# Neutral fallback colours when palette data is absent
_FALLBACK_BG = (28, 22, 18)       # dark sepia
_FALLBACK_FG = (200, 175, 128)    # warm sand

# Cover-specific overrides (used when no adjacent shot provides colours)
_COVER_BG     = (18, 18, 18)
_COVER_FG_DIM = (140, 130, 120)   # muted — cover title is secondary to the film

# Font paths
_FONTS_DIR = Path(__file__).parent.parent / "styles" / "fonts"
_LC_DIR    = _FONTS_DIR / "libre_clarendon" / "fonts"

_FONT_FLIPBOOK = str(_LC_DIR / "LibreClarendonNormal-110Medium.otf")
_FONT_COVER    = str(_LC_DIR / "LibreClarendonNormal-68Regular.otf")

_FONT_FALLBACKS = [
    str(_FONTS_DIR / "Hanken_Grotesk" / "HankenGrotesk-VariableFont_wght.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]

# Horizontal margin as a fraction of page width for word rendering
_MARGIN_FRAC = 0.10


# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

def _load_font(size: int, preferred_path: str) -> ImageFont.FreeTypeFont:
    """Load a Pillow font at *size*, falling back gracefully."""
    for path in [preferred_path] + _FONT_FALLBACKS:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None  # type: ignore[return-value]


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: Any) -> tuple[int, int]:
    """Return (width, height) of rendered text."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        # Pillow < 9 fallback
        return draw.textsize(text, font=font)  # type: ignore[attr-defined]


def _fit_font(
    text: str,
    max_width: int,
    max_height: int,
    preferred_path: str,
    probe_image: Image.Image,
) -> tuple[Any, int, int]:
    """Binary-search the largest font size where *text* fits within the box.

    Returns (font, text_width, text_height).
    """
    draw = ImageDraw.Draw(probe_image)
    lo, hi = 12, 600
    best_font = _load_font(lo, preferred_path)
    best_w, best_h = _measure_text(draw, text, best_font)

    for _ in range(14):  # 14 iterations → ≈ 0.006 px precision
        mid = (lo + hi) // 2
        font = _load_font(mid, preferred_path)
        if font is None:
            break
        w, h = _measure_text(draw, text, font)
        if w <= max_width and h <= max_height:
            best_font = font
            best_w, best_h = w, h
            lo = mid + 1
        else:
            hi = mid - 1

    return best_font, best_w, best_h


# ---------------------------------------------------------------------------
# Page data model
# ---------------------------------------------------------------------------

def _make_shot_page(
    entry: dict,
    palette_shot: Optional[dict],
    shot_index: int,
) -> dict:
    """Build a page descriptor dict for one shot annotation entry.

    Combines motif from the annotation with palette colors.
    Uses neutral fallbacks when data is absent.
    """
    shot_data = entry.get("shot", {}) if isinstance(entry, dict) else {}

    # Motif
    motif_obj = shot_data.get("motif") if isinstance(shot_data, dict) else None
    if isinstance(motif_obj, dict):
        motif_value = (motif_obj.get("value") or "").strip()
    elif isinstance(motif_obj, str):
        motif_value = motif_obj.strip()
    else:
        motif_value = ""
    if not motif_value:
        motif_value = "—"

    # Palette colors
    if palette_shot and isinstance(palette_shot, dict):
        bg_raw = palette_shot.get("background", {})
        fg_raw = palette_shot.get("foreground", {})
        bg_rgb = list(bg_raw.get("rgb") or _FALLBACK_BG)
        fg_rgb = list(fg_raw.get("rgb") or _FALLBACK_FG)
    else:
        bg_rgb = list(_FALLBACK_BG)
        fg_rgb = list(_FALLBACK_FG)

    # Shot metadata
    shot_id   = str(shot_data.get("shot_id", "")) if isinstance(shot_data, dict) else ""
    start_time = ""
    if palette_shot:
        start_time = palette_shot.get("start_time", "") or ""

    return {
        "kind":        "shot",
        "text":        motif_value,
        "motif":       motif_value,
        "bg_rgb":      bg_rgb,
        "fg_rgb":      fg_rgb,
        "shot_id":     shot_id,
        "shot_index":  shot_index,
        "start_time":  start_time,
    }


def _make_cover_page(kind: str, text: str, bg_rgb: list, fg_rgb: list) -> dict:
    return {
        "kind":       kind,
        "text":       text,
        "motif":      text,
        "bg_rgb":     bg_rgb,
        "fg_rgb":     fg_rgb,
        "shot_id":    None,
        "shot_index": None,
        "start_time": "",
    }


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_flipbook_data(
    project_path: str,
    filename: str,
    media_type: str = "movies",
) -> dict:
    """Load and join annotation + palette data for a movie.

    Returns a dict with keys:
      ``pages``     — ordered list of page descriptor dicts (cover + shots + cover)
      ``title``     — movie title
      ``year``      — movie year
      ``filename``  — source video filename

    Pages are ordered: front_cover, shot_0, shot_1, …, shot_N, back_cover.

    Raises:
        FileNotFoundError: If the annotation JSON does not exist.
    """
    from data.annotate import get_annotation_json_path
    from data.metadata import get_metadata

    # Load annotation JSON
    json_path = get_annotation_json_path(project_path, filename, media_type)
    if not json_path.exists():
        raise FileNotFoundError(
            f"No annotation JSON found: {json_path}\n"
            f"  Run: crossing annotate shot --movie '{filename}' first."
        )
    entries: list = json.loads(json_path.read_text(encoding="utf-8"))

    # Load palette JSON (best-effort — may be absent)
    from data.palette import get_palette_path
    palette_path = get_palette_path(project_path, filename, media_type)
    palette_by_id: dict[str, dict] = {}
    palette_by_index: dict[int, dict] = {}
    if palette_path.exists():
        try:
            palette_doc = json.loads(palette_path.read_text(encoding="utf-8"))
            for ps in palette_doc.get("shots", []):
                sid = str(ps.get("shot_id", ""))
                idx = ps.get("shot_index")
                if sid:
                    palette_by_id[sid] = ps
                if idx is not None:
                    palette_by_index[int(idx)] = ps
        except Exception:
            pass

    # Movie metadata
    meta_entries = get_metadata(project_path, media_type=media_type)
    meta = next((e for e in meta_entries if e.get("filename") == filename), {})
    title = meta.get("title") or Path(filename).stem
    year  = str(meta.get("year") or "")

    # Build shot pages
    shot_pages: list[dict] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        shot_data = entry.get("shot", {}) if isinstance(entry, dict) else {}
        shot_id = str(shot_data.get("shot_id", "")) if isinstance(shot_data, dict) else ""

        # Join palette by shot_id first, then by index
        palette_shot = palette_by_id.get(shot_id) or palette_by_index.get(i)

        page = _make_shot_page(entry, palette_shot, i)
        shot_pages.append(page)

    # Cover colors: use the first shot's colors for the front cover,
    # and the last shot's colors for the back cover.
    first_shot = shot_pages[0] if shot_pages else None
    last_shot  = shot_pages[-1] if shot_pages else None

    front_bg = first_shot["bg_rgb"] if first_shot else list(_COVER_BG)
    front_fg = first_shot["fg_rgb"] if first_shot else list(_COVER_FG_DIM)
    back_bg  = last_shot["bg_rgb"]  if last_shot  else list(_COVER_BG)
    back_fg  = last_shot["fg_rgb"]  if last_shot  else list(_COVER_FG_DIM)

    front_cover = _make_cover_page("cover_front", title, front_bg, front_fg)
    back_cover  = _make_cover_page("cover_back",  year,  back_bg,  back_fg)

    pages = [front_cover] + shot_pages + [back_cover]

    return {
        "pages":    pages,
        "title":    title,
        "year":     year,
        "filename": filename,
    }


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

def render_flipbook_page(page: dict) -> Image.Image:
    """Render one flipbook page as a PIL Image (RGB, PAGE_W × PAGE_H).

    The background is filled with ``page["bg_rgb"]``.
    The text (motif word, title, or year) is drawn large and centered
    in ``page["fg_rgb"]`` using Libre Clarendon Normal Medium.
    """
    bg = tuple(int(v) for v in page["bg_rgb"])
    fg = tuple(int(v) for v in page["fg_rgb"])
    text = str(page.get("text") or "")

    img  = Image.new("RGB", (PAGE_W, PAGE_H), color=bg)
    draw = ImageDraw.Draw(img)

    if not text:
        return img

    kind = page.get("kind", "shot")
    font_path = _FONT_COVER if kind in ("cover_front", "cover_back") else _FONT_FLIPBOOK

    # Available area with horizontal margins
    margin_x = int(PAGE_W * _MARGIN_FRAC)
    margin_y = int(PAGE_H * 0.15)
    avail_w  = PAGE_W - 2 * margin_x
    avail_h  = PAGE_H - 2 * margin_y

    # Probe image for font measurement
    probe = Image.new("RGB", (1, 1))

    font, tw, th = _fit_font(text, avail_w, avail_h, font_path, probe)
    if font is None:
        return img

    # Centered position
    x = (PAGE_W - tw) // 2
    y = (PAGE_H - th) // 2

    draw.text((x, y), text, font=font, fill=fg)
    return img


def render_flipbook_cover(kind: str, text: str, bg_rgb: list, fg_rgb: list) -> Image.Image:
    """Convenience wrapper — render a cover page image."""
    page = _make_cover_page(kind, text, bg_rgb, fg_rgb)
    return render_flipbook_page(page)


# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------

def get_flipbook_output_path(project_path: str, filename: str) -> Path:
    """Return the canonical PDF output path for a flipbook.

    ``<project>/output/flipbooks/<stem>-flipbook.pdf``
    """
    stem = Path(filename).stem
    return Path(project_path) / "output" / "flipbooks" / f"{stem}-flipbook.pdf"


def export_flipbook_pdf(
    pages: list[dict],
    output_path: Path,
    *,
    title: str = "",
    verbose: bool = False,
) -> Path:
    """Render all pages and save a multi-page PDF.

    Parameters
    ----------
    pages:       List of page descriptor dicts (from ``load_flipbook_data``).
    output_path: Destination PDF path.
    title:       Optional PDF metadata title.
    verbose:     Print progress.

    Returns
    -------
    *output_path* after saving.
    """
    if not pages:
        raise ValueError("No pages to render.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    images: list[Image.Image] = []
    for i, page in enumerate(pages):
        img = render_flipbook_page(page)
        images.append(img)
        if verbose:
            kind = page.get("kind", "shot")
            if kind == "cover_front":
                print(f"  cover front: {page.get('text', '')}")
            elif kind == "cover_back":
                print(f"  cover back:  {page.get('text', '')}")
            else:
                idx = page.get("shot_index", i)
                motif = page.get("text", "")
                print(f"  page {idx + 1:03d}: {motif}")

    if not images:
        raise ValueError("No images rendered.")

    pdfinfo = {
        "title":    title,
        "author":   "Crossing Tool",
        "subject":  f"Flipbook — {title}",
        "creator":  "generators.flipbook",
        "creationdate": datetime.now(timezone.utc),
    }
    images[0].save(
        str(output_path),
        "PDF",
        save_all=True,
        append_images=images[1:],
        resolution=150,
        pdfinfo=pdfinfo,
    )

    return output_path


# ---------------------------------------------------------------------------
# Movie-level entry points
# ---------------------------------------------------------------------------

def generate_flipbook_for_movie(
    project_path: str,
    filename: str,
    media_type: str = "movies",
    *,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Build and save a flipbook PDF for one movie.

    Parameters
    ----------
    project_path: Project root directory.
    filename:     Video filename.
    media_type:   ``"movies"`` or ``"gameplay"``.
    force:        Overwrite existing PDF.
    verbose:      Print page progress.

    Returns
    -------
    dict with ``filename``, ``pages``, ``output_path``, ``status``.

    Raises
    ------
    FileNotFoundError: If annotation JSON is absent.
    FileExistsError:   If PDF already exists and *force* is False.
    """
    output_path = get_flipbook_output_path(project_path, filename)

    if output_path.exists() and not force:
        raise FileExistsError(
            f"Flipbook already exists: {output_path}\n"
            "  Use --force to regenerate."
        )

    data = load_flipbook_data(project_path, filename, media_type)
    pages = data["pages"]

    export_flipbook_pdf(pages, output_path, title=data["title"], verbose=verbose)

    return {
        "filename":    filename,
        "title":       data["title"],
        "pages":       len(pages),
        "output_path": str(output_path),
        "status":      "ok",
    }


def generate_flipbook_for_all_movies(
    project_path: str,
    media_type: str = "movies",
    *,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Generate flipbooks for all movies that have an annotation JSON.

    Returns aggregate summary dict.
    """
    from data.metadata import get_metadata
    from data.annotate import get_annotation_json_path

    meta_entries = get_metadata(project_path, media_type=media_type)

    total_files     = 0
    total_processed = 0
    total_skipped   = 0
    total_failed    = 0
    errors: list    = []

    for meta in meta_entries:
        filename = meta.get("filename")
        if not filename:
            continue

        json_path = get_annotation_json_path(project_path, filename, media_type)
        if not json_path.exists():
            continue

        total_files += 1
        title = meta.get("title") or filename

        if verbose:
            print(f"\n{title}")
        else:
            print(f"  {title}...", end=" ", flush=True)

        try:
            summary = generate_flipbook_for_movie(
                project_path, filename, media_type,
                force=force, verbose=verbose,
            )
            total_processed += 1
            if not verbose:
                print(f"ok  ({summary['pages']} pages)")
        except FileExistsError as exc:
            total_skipped += 1
            if not verbose:
                print("skip (exists — use --force)")
        except FileNotFoundError as exc:
            total_files -= 1
            if not verbose:
                print("skip (no annotations)")
        except Exception as exc:
            errors.append((filename, str(exc)))
            total_failed += 1
            if not verbose:
                print(f"error: {exc}")

    return {
        "total_files":     total_files,
        "total_processed": total_processed,
        "total_skipped":   total_skipped,
        "total_failed":    total_failed,
        "errors":          errors,
    }
