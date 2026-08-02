"""Palette PDF generator — colour-swatch contact sheet for one movie.

Renders every shot's foreground/background colour pair from a cached
palette JSON (see ``data/palette.py``) as a single-page contact sheet: a
best-fit grid of swatches, each a filled rectangle in the shot's background
colour with a circle in the shot's foreground colour — the same visual
design as the Palette Visualizer's shot swatches (see
``visualizers/palette_visualizer.py::_ShotCell``), without the interactive
debug affordances (warning dots, palette strips).

This is a renderer, not a semantic generator — it consumes the palette
cache produced by ``crossing index palette create`` and does not compute
colours itself.

Output
------
PDF: ``<project>/output/palettes/<stem>-palette.pdf``
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canvas dimensions (logical pixels — suitable for PDF at 150 dpi)
PAGE_W = 1920
PAGE_H = 1080

_MARGIN = 20      # px — page outer margin
_GAP    = 6       # px — gap between swatches
_ASPECT = 16 / 9  # swatch width : height ratio, matching the visualizer

_BG_COLOR      = (18, 18, 18)      # page background
_FALLBACK_BG   = (60, 60, 60)      # shot background fallback
_FALLBACK_FG   = (180, 180, 180)   # shot foreground fallback


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

def render_palette_page(shots: list[dict]) -> Image.Image:
    """Render a best-fit grid of shot colour swatches as a single PIL image.

    Uses the same best-fit-column-count search as
    ``visualizers.components.aspect_grid.AspectGridWidget`` at zoom 1.0, so
    the printed layout matches what the visualizer shows on screen.
    """
    if not shots:
        raise ValueError("No shots to render.")

    n = len(shots)
    avail_w = PAGE_W - 2 * _MARGIN
    avail_h = PAGE_H - 2 * _MARGIN

    best_cols, best_cw = 1, 0.0
    for cols in range(1, n + 1):
        rows = math.ceil(n / cols)
        cw = (avail_w - (cols - 1) * _GAP) / cols
        ch = cw / _ASPECT
        total_h = rows * ch + (rows - 1) * _GAP
        if cw <= 0 or total_h > avail_h:
            continue
        if cw > best_cw:
            best_cw = cw
            best_cols = cols

    cols = best_cols
    rows = math.ceil(n / cols)
    cw = (avail_w - (cols - 1) * _GAP) / cols
    ch = cw / _ASPECT

    grid_w = cols * cw + (cols - 1) * _GAP
    grid_h = rows * ch + (rows - 1) * _GAP
    x0 = (PAGE_W - grid_w) / 2
    y0 = (PAGE_H - grid_h) / 2

    img = Image.new("RGB", (PAGE_W, PAGE_H), color=_BG_COLOR)
    draw = ImageDraw.Draw(img)

    for i, shot in enumerate(shots):
        col = i % cols
        row = i // cols
        cx0 = x0 + col * (cw + _GAP)
        cy0 = y0 + row * (ch + _GAP)
        cx1 = cx0 + cw
        cy1 = cy0 + ch

        bg_rgb = tuple(int(v) for v in (shot.get("background", {}).get("rgb") or _FALLBACK_BG))
        fg_rgb = tuple(int(v) for v in (shot.get("foreground", {}).get("rgb") or _FALLBACK_FG))

        draw.rectangle([cx0, cy0, cx1 - 1, cy1 - 1], fill=bg_rgb)

        # Japanese-flag ratio: circle diameter = 3/5 of cell height,
        # matching _ShotCell.paintEvent().
        diameter = ch * 3 / 5
        ccx = (cx0 + cx1) / 2
        ccy = (cy0 + cy1) / 2
        draw.ellipse(
            [ccx - diameter / 2, ccy - diameter / 2, ccx + diameter / 2, ccy + diameter / 2],
            fill=fg_rgb,
        )

    return img


# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------

def get_palette_output_path(project_path: str, filename: str) -> Path:
    """Return the canonical PDF output path for a palette contact sheet.

    ``<project>/output/palettes/<stem>-palette.pdf``
    """
    stem = Path(filename).stem
    return Path(project_path) / "output" / "palettes" / f"{stem}-palette.pdf"


def export_palette_pdf(
    shots: list[dict],
    output_path: "str | Path",
    *,
    title: str = "",
    verbose: bool = False,
) -> Path:
    """Render the shot swatch grid and save a single-page PDF.

    Parameters
    ----------
    shots:       List of shot palette dicts (from a palette cache's ``shots``).
    output_path: Destination PDF path.
    title:       Optional PDF metadata title.
    verbose:     Print progress.

    Returns
    -------
    *output_path* after saving.
    """
    if not shots:
        raise ValueError("No shots to render.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    img = render_palette_page(shots)
    if verbose:
        print(f"  {len(shots)} shot(s) rendered")

    pdfinfo = {
        "title":    title,
        "author":   "Crossing Tool",
        "subject":  f"Palette — {title}",
        "creator":  "generators.palette",
        "creationdate": datetime.now(timezone.utc),
    }
    img.save(str(output_path), "PDF", resolution=150, pdfinfo=pdfinfo)

    return output_path


# ---------------------------------------------------------------------------
# Movie-level entry points
# ---------------------------------------------------------------------------

def generate_palette_pdf_for_movie(
    project_path: str,
    filename: str,
    media_type: str = "movie",
    *,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Build and save a palette contact-sheet PDF for one movie.

    Parameters
    ----------
    project_path: Project root directory.
    filename:     Video filename.
    media_type:   ``"movie"`` or ``"gameplay"``.
    force:        Overwrite existing PDF.
    verbose:      Print rendering progress.

    Returns
    -------
    dict with ``filename``, ``title``, ``shots``, ``output_path``, ``status``.

    Raises
    ------
    FileNotFoundError: If no palette cache exists for this movie.
    FileExistsError:   If PDF already exists and *force* is False.
    """
    from data.metadata import get_metadata
    from data.palette import load_palette

    output_path = get_palette_output_path(project_path, filename)

    if output_path.exists() and not force:
        raise FileExistsError(
            f"Palette PDF already exists: {output_path}\n"
            "  Use --force to regenerate."
        )

    data = load_palette(project_path, filename, media_type)
    if not data:
        raise FileNotFoundError(
            f"No palette cache found for {filename!r}.\n"
            f"  Run: crossing index palette create --title '{filename}' first."
        )
    shots = data.get("shots", [])
    if not shots:
        raise ValueError(f"No shots with palette data for {filename!r}.")

    meta_entries = get_metadata(project_path, media_type=media_type)
    meta = next((e for e in meta_entries if e.get("filename") == filename), {})
    title = meta.get("title") or Path(filename).stem

    export_palette_pdf(shots, output_path, title=title, verbose=verbose)

    return {
        "filename":    filename,
        "title":       title,
        "shots":       len(shots),
        "output_path": str(output_path),
        "status":      "ok",
    }


def generate_palette_pdf_for_all_movies(
    project_path: str,
    media_type: str = "movie",
    *,
    force: bool = False,
    verbose: bool = False,
    on_item_done=None,
) -> dict:
    """Generate palette PDFs for every movie that has a palette cache.

    Returns aggregate summary dict.
    """
    from data.metadata import get_metadata
    from data.palette import get_palette_path

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

        palette_path = get_palette_path(project_path, filename, media_type)
        if not palette_path.exists():
            continue

        total_files += 1
        title = meta.get("title") or filename

        if verbose:
            print(f"\n{title}")
        else:
            print(f"  {title}...", end=" ", flush=True)

        try:
            summary = generate_palette_pdf_for_movie(
                project_path, filename, media_type,
                force=force, verbose=verbose,
            )
            total_processed += 1
            if not verbose:
                print(f"ok  ({summary['shots']} shots)")
            if on_item_done is not None:
                on_item_done(title, summary, None)
        except FileExistsError:
            total_skipped += 1
            if not verbose:
                print("skip (exists — use --force)")
        except FileNotFoundError:
            total_files -= 1
            if not verbose:
                print("skip (no palette cache)")
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
