"""Contact-sheet rendering for corpus curation and review.

Used twice: to inspect large candidate pools during selection, and to produce
the final reviewable corpus sheets.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from generators._common import load_font_with_fallback

BG = (16, 16, 18)
FG = (240, 240, 240)
DIM = (155, 155, 162)
ACCENT = (232, 176, 92)


def _font(size: int):
    return load_font_with_fallback(size)


def _fit(path: str, width: int, height: int) -> Image.Image:
    cell = Image.new("RGB", (width, height), (0, 0, 0))
    try:
        with Image.open(path) as opened:
            image = opened.convert("RGB")
    except Exception:
        ImageDraw.Draw(cell).text((8, height // 2), "missing", font=_font(16), fill=(200, 60, 60))
        return cell
    scale = min(width / image.width, height / image.height)
    resized = image.resize(
        (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
        Image.LANCZOS,
    )
    cell.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    return cell


def sheet(
    cells: list[dict],
    path: Path,
    *,
    title: str = "",
    columns: int = 5,
    cell_width: int = 420,
    cell_height: int = 236,
    caption_height: int = 76,
) -> Path:
    """Render a labelled grid.

    Each cell dict takes ``image`` (path) plus optional ``id``, ``line1``,
    ``line2``, ``tags``.
    """
    gutter = 10
    rows = (len(cells) + columns - 1) // columns
    header = 74 if title else gutter
    block_h = cell_height + caption_height
    canvas = Image.new(
        "RGB",
        (
            columns * cell_width + gutter * (columns + 1),
            header + rows * (block_h + gutter) + gutter,
        ),
        BG,
    )
    draw = ImageDraw.Draw(canvas)
    if title:
        draw.text((gutter + 6, 24), title, font=_font(30), fill=FG)

    id_font = _font(22)
    line_font = _font(17)
    tag_font = _font(15)

    for order, cell in enumerate(cells):
        col = order % columns
        row = order // columns
        x = gutter + col * (cell_width + gutter)
        y = header + row * (block_h + gutter)

        canvas.paste(_fit(cell.get("image", ""), cell_width, cell_height), (x, y))

        cy = y + cell_height + 6
        ident = cell.get("id", "")
        if ident:
            draw.text((x + 4, cy), ident, font=id_font, fill=ACCENT)
        draw.text((x + 4 + (92 if ident else 0), cy + 2), cell.get("line1", "")[:44], font=line_font, fill=FG)
        draw.text((x + 4, cy + 26), cell.get("line2", "")[:62], font=line_font, fill=DIM)
        tags = cell.get("tags") or []
        if tags:
            draw.text((x + 4, cy + 50), "  ".join(tags)[:66], font=tag_font, fill=(120, 170, 210))

    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, format="JPEG", quality=88)
    return path
