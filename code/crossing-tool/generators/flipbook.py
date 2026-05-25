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
    "text":        str,          # motif word / semantic title / "Title, YYYY"
    "bg_rgb":      [R, G, B],
    "fg_rgb":      [R, G, B],    # always black for covers
    "shot_id":     str | None,
    "shot_index":  int | None,
    "start_time":  str,
    "motif":       str,          # copy of text for shot pages

    # cover_front only:
    "film_motif":  dict | {},    # full film_motif metadata

    # cover_back only:
    "back_title":  str,          # original movie title (Bold)
    "back_year":   str,          # year string (Light suffix)
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

# Primary typography hierarchy
_FONT_BOLD    = str(_LC_DIR / "LibreClarendonNormal-162Bold.otf")   # front/back cover titles
_FONT_LIGHT   = str(_LC_DIR / "LibreClarendonNormal-42Light.otf")   # back cover year suffix
_FONT_REGULAR = str(_LC_DIR / "LibreClarendonNormal-68Regular.otf") # interior shot pages

# Legacy aliases (kept for any external callers)
_FONT_FLIPBOOK = _FONT_REGULAR
_FONT_COVER    = _FONT_BOLD

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
    """Return (width, height) of rendered text using tight ink bounds."""
    try:
        # font.getbbox returns the tight ink bounding box without line-height
        # padding — more accurate for centering and size-fitting than textbbox.
        bbox = font.getbbox(text)  # type: ignore[union-attr]
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        pass
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
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
    motif_shot: Optional[dict] = None,
) -> dict:
    """Build a page descriptor dict for one shot annotation entry.

    Combines motif from the motif file (preferred) or annotation with palette
    colors.  Uses neutral fallbacks when data is absent.
    """
    shot_data = entry.get("shot", {}) if isinstance(entry, dict) else {}

    # Motif: prefer separate motif file, fall back to value embedded in annotation
    if isinstance(motif_shot, dict):
        motif_value = (motif_shot.get("value") or "").strip()
    else:
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
# Per-kind rendering helpers
# ---------------------------------------------------------------------------

def _getbbox_safe(font: Any, text: str, draw: ImageDraw.ImageDraw) -> tuple:
    """Return (bx, by, tw, th) tight ink bounds for *text* at *font*.

    Falls back gracefully across Pillow versions.
    """
    try:
        bb = font.getbbox(text)  # Pillow ≥ 8
        return bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]
    except AttributeError:
        pass
    try:
        bb = draw.textbbox((0, 0), text, font=font)
        return bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]
    except AttributeError:
        w, h = draw.textsize(text, font=font)  # type: ignore[attr-defined]
        return 0, 0, w, h


def _render_shot_text(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    page: dict,
) -> None:
    """Render the motif word centered on a shot page (68 Regular, fg color)."""
    text   = str(page.get("text") or "")
    fg_raw = page.get("fg_rgb") or list(_FALLBACK_FG)
    fg     = tuple(int(v) for v in fg_raw)

    if not text:
        return

    margin_x = int(PAGE_W * _MARGIN_FRAC)
    margin_y = int(PAGE_H * 0.15)
    avail_w  = PAGE_W - 2 * margin_x
    avail_h  = PAGE_H - 2 * margin_y

    probe = Image.new("RGB", (1, 1))
    font, _, _ = _fit_font(text, avail_w, avail_h, _FONT_REGULAR, probe)
    if font is None:
        return

    bx, by, tw, th = _getbbox_safe(font, text, draw)
    x = (PAGE_W - tw) // 2 - bx
    y = (PAGE_H - th) // 2 - by
    draw.text((x, y), text, font=font, fill=fg)


def _render_cover_front(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    page: dict,
) -> None:
    """Render the front cover: semantic title in 162 Bold, black, slightly low.

    Text ink center is placed at 58 % of page height (not geometric center).
    This gives the title visual weight without feeling bottom-heavy.
    """
    text = str(page.get("text") or "")
    if not text:
        return

    fg = (0, 0, 0)  # always black

    margin_x = int(PAGE_W * _MARGIN_FRAC)
    margin_y = int(PAGE_H * 0.15)
    avail_w  = PAGE_W - 2 * margin_x
    avail_h  = PAGE_H - 2 * margin_y

    probe = Image.new("RGB", (1, 1))
    font, _, _ = _fit_font(text, avail_w, avail_h, _FONT_BOLD, probe)
    if font is None:
        return

    bx, by, tw, th = _getbbox_safe(font, text, draw)

    # Ink center at 58 % of page height
    ink_center_y = int(PAGE_H * 0.58)
    x = (PAGE_W - tw) // 2 - bx
    y = ink_center_y - th // 2 - by
    draw.text((x, y), text, font=font, fill=fg)


def _render_cover_back(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    page: dict,
) -> None:
    """Render the back cover: title in 162 Bold + ", YYYY" in 42 Light.

    Both parts share the same draw y (baseline-aligned in Pillow's coordinate
    system).  The combined ink block is centered horizontally and vertically.
    Text color is very dark grey (20, 20, 20).
    """
    back_title = str(page.get("back_title") or page.get("text") or "")
    back_year  = str(page.get("back_year")  or "")
    suffix     = f", {back_year}" if back_year else ""

    if not back_title:
        return

    fg = (20, 20, 20)  # very dark grey for back cover

    margin_x = int(PAGE_W * _MARGIN_FRAC)
    avail_w  = PAGE_W - 2 * margin_x
    avail_h  = int(PAGE_H * 0.70)

    probe = Image.new("RGB", (1, 1))

    # Fit the combined text using Bold as proxy (Bold is wider → conservative
    # size; the Light suffix will definitely fit at the resulting pt size).
    combined_proxy = back_title + suffix
    font_bold, _, _ = _fit_font(combined_proxy, avail_w, avail_h, _FONT_BOLD, probe)
    if font_bold is None:
        return

    pt_size   = font_bold.size
    font_light = _load_font(pt_size, _FONT_LIGHT)

    # Measure each part
    bx1, by1, tw1, th1 = _getbbox_safe(font_bold,  back_title, draw)
    if suffix:
        bx2, by2, tw2, th2 = _getbbox_safe(font_light, suffix, draw)
    else:
        bx2, by2, tw2, th2 = 0, 0, 0, 0

    # Combined ink extents (both parts at the same draw y)
    total_ink_w = tw1 + tw2
    top_rel     = min(by1, by2) if suffix else by1
    bottom_rel  = max(by1 + th1, by2 + th2) if suffix else (by1 + th1)

    # draw_y such that the union ink block is vertically centered
    draw_y = (PAGE_H - top_rel - bottom_rel) // 2

    # draw_x such that combined ink is horizontally centered
    draw_x = (PAGE_W - total_ink_w) // 2 - bx1

    draw.text((draw_x, draw_y), back_title, font=font_bold,  fill=fg)
    if suffix:
        draw_x2 = draw_x + bx1 + tw1 - bx2
        draw.text((draw_x2, draw_y), suffix, font=font_light, fill=fg)


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

    # Load motif doc (best-effort — may be absent until motifs are generated)
    from data.motif import load_motif_doc as _load_motif_doc
    motif_doc = _load_motif_doc(project_path, filename, media_type)
    motif_by_id: dict[str, dict] = {}
    motif_by_index: dict[int, dict] = {}
    for _idx, _ms in enumerate(motif_doc.get("shots", [])):
        if isinstance(_ms, dict):
            _sid = str(_ms.get("shot_id", ""))
            if _sid:
                motif_by_id[_sid] = _ms
            motif_by_index[_idx] = _ms

    # Movie metadata
    meta_entries = get_metadata(project_path, media_type=media_type)
    meta = next((e for e in meta_entries if e.get("filename") == filename), {})
    title    = meta.get("title") or Path(filename).stem
    year     = str(meta.get("year") or "")
    media_id = str(meta.get("media_id") or "")

    # Load ignored shot IDs from shotlist (best-effort — may be absent)
    ignored_shot_ids: set[str] = set()
    try:
        from data.shotlist import read_shotlist, attach_shot_ids
        sl_shots = read_shotlist(project_path, filename, media_type)
        # Fallback: attach IDs from frame data for CSVs that predate the shot_id column
        if media_id and not any(s.get("shot_id") for s in sl_shots):
            attach_shot_ids(sl_shots, media_id)
        for sl_shot in sl_shots:
            if sl_shot.get("Ignore", "No") == "Yes":
                sid = str(sl_shot.get("shot_id", ""))
                if sid:
                    ignored_shot_ids.add(sid)
    except Exception:
        pass

    # Build shot pages (skip ignored shots)
    shot_pages: list[dict] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        shot_data = entry.get("shot", {}) if isinstance(entry, dict) else {}
        shot_id = str(shot_data.get("shot_id", "")) if isinstance(shot_data, dict) else ""

        if shot_id in ignored_shot_ids:
            continue

        # Join palette by shot_id first, then by index
        palette_shot = palette_by_id.get(shot_id) or palette_by_index.get(i)

        # Join motif by shot_id first, then by annotation entry index
        motif_shot = motif_by_id.get(shot_id) or motif_by_index.get(i)

        page = _make_shot_page(entry, palette_shot, i, motif_shot)
        shot_pages.append(page)

    # Cover colors: use the first title-card shot for the front cover
    # (motif == "title" among the first TITLE_CARD_LOOKAHEAD non-ignored shots),
    # and the "the end" shot for the back cover, falling back to first/last.
    _TITLE_CARD_LOOKAHEAD = 10

    front_shot: Optional[dict] = None
    for p in shot_pages[:_TITLE_CARD_LOOKAHEAD]:
        if p.get("motif", "").strip().lower() == "title":
            front_shot = p
            break
    if front_shot is None:
        front_shot = shot_pages[0] if shot_pages else None

    back_shot: Optional[dict] = None
    for p in shot_pages:
        if p.get("motif", "").strip().lower() == "the end":
            back_shot = p
            break
    if back_shot is None:
        back_shot = shot_pages[-1] if shot_pages else None

    front_bg = front_shot["bg_rgb"] if front_shot else list(_COVER_BG)
    back_bg  = back_shot["bg_rgb"]  if back_shot  else list(_COVER_BG)

    # Film title motif from the shared motif doc (best-effort — may be absent until generated)
    film_motif = motif_doc.get("title") or {}

    # Front cover: semantic condensation title (or original title as fallback)
    front_text = film_motif.get("value", "").strip() or title
    front_cover: dict = {
        "kind":       "cover_front",
        "text":       front_text,
        "motif":      front_text,
        "bg_rgb":     front_bg,
        "fg_rgb":     [0, 0, 0],   # always black
        "shot_id":    None,
        "shot_index": None,
        "start_time": "",
        "film_motif": film_motif,
    }

    # Back cover: archival identity — original title + year, stratified
    back_text = f"{title}, {year}" if year else title
    back_cover: dict = {
        "kind":       "cover_back",
        "text":       back_text,
        "motif":      back_text,
        "back_title": title,
        "back_year":  year,
        "bg_rgb":     back_bg,
        "fg_rgb":     [20, 20, 20],   # very dark grey
        "shot_id":    None,
        "shot_index": None,
        "start_time": "",
    }

    pages = [front_cover] + shot_pages + [back_cover]

    return {
        "pages":      pages,
        "title":      title,
        "year":       year,
        "filename":   filename,
        "film_motif": film_motif,
    }


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

def render_flipbook_page(page: dict) -> Image.Image:
    """Render one flipbook page as a PIL Image (RGB, PAGE_W × PAGE_H).

    Dispatches to per-kind helpers:
      ``cover_front`` — semantic title in 162 Bold, black, slightly low
      ``cover_back``  — archival identity: title (162 Bold) + ", YYYY" (42 Light)
      ``shot``        — motif word in 68 Regular, palette foreground color, centered
    """
    bg  = tuple(int(v) for v in page["bg_rgb"])
    img = Image.new("RGB", (PAGE_W, PAGE_H), color=bg)
    draw = ImageDraw.Draw(img)
    kind = page.get("kind", "shot")

    if kind == "cover_front":
        _render_cover_front(img, draw, page)
    elif kind == "cover_back":
        _render_cover_back(img, draw, page)
    else:
        _render_shot_text(img, draw, page)

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
    on_item_done=None,
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
            if on_item_done is not None:
                on_item_done(title, summary, None)
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
            if on_item_done is not None:
                on_item_done(title, None, exc)

    return {
        "total_files":     total_files,
        "total_processed": total_processed,
        "total_skipped":   total_skipped,
        "total_failed":    total_failed,
        "errors":          errors,
    }
