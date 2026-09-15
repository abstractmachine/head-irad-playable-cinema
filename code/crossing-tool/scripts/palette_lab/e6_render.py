"""Static candidate contact sheets for the E6 human review.

The sheet is deliberately blind: it shows measured candidates only and never
the E5 ranking, score, or selection, so the reviewer is not anchored to the
baseline they are being asked to disagree with.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from scripts.palette_lab import render as R


BG = (16, 16, 18)
FG = (238, 238, 238)
DIM = (152, 152, 160)
KEY = (120, 170, 235)
MARK = (250, 210, 90)
OUTLINE = (255, 255, 255)

PANEL_W = 440
COLUMNS = 4
GAP = 10
PAGE_W = PANEL_W * COLUMNS + GAP * (COLUMNS - 1)
ROWS_PER_PAGE = 3


def _font(size: int):
    return R._font(size)


def _wrap(value: str, limit: int, lines: int) -> list[str]:
    words = str(value or "").split()
    out, current = [], ""
    for word in words:
        if current and len(current) + len(word) + 1 > limit:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    out = out or [""]
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1][: max(0, limit - 1)] + "\u2026"
    return out


def _contour(mask_layer: Image.Image) -> Image.Image:
    """Outline the mask so a near-black candidate is still locatable."""
    mask = np.asarray(mask_layer) > 127
    edge = np.zeros_like(mask)
    for axis in (0, 1):
        for shift in (1, -1):
            edge |= mask ^ np.roll(mask, shift, axis=axis)
    edge &= mask
    return Image.fromarray(edge.astype(np.uint8) * 255, mode="L")


def _candidate_panel(base: Image.Image, dimmed: Image.Image, mask_layer: Image.Image,
                     record: dict) -> Image.Image:
    """One candidate: its pixels shown in its own measured colour."""
    colour = Image.new("RGB", base.size, tuple(record["rgb"]))
    painted = Image.composite(Image.blend(base, colour, 0.72), dimmed, mask_layer)
    painted.paste(Image.new("RGB", base.size, OUTLINE), (0, 0), _contour(mask_layer))
    outline = Image.new("RGB", (base.width, base.height + 4), MARK)
    outline.paste(painted, (0, 4))

    if record["source"] == "semantic":
        detail = [
            ("role", str(record["semantic_role"] or "")),
            ("phrase", record["label"]),
            ("declared", f"{record['declared_location']} / {record['declared_extent']}"),
        ]
    else:
        detail = [
            ("level", f"{record['hierarchy_level']} regions"),
            ("region", str(record["hierarchy_region"])),
            ("declared", "\u2014 bottom-up region, no phrase"),
        ]
    rows = [
        ("colour", f"{record['hex']}   rgb {tuple(record['rgb'])}"),
        ("source", record["source"]),
        *detail,
        ("area", f"{record['area_percent']}%   ({record['area_pixels']} px)"),
        ("location", f"{record['observed_cell']}   centroid {record['centroid']}"),
    ]
    caption = Image.new("RGB", (base.width, 54 + 26 * len(rows) + 12), BG)
    draw = ImageDraw.Draw(caption)
    swatch = 34
    draw.rectangle((12, 10, 12 + swatch, 10 + swatch), fill=(235, 235, 235))
    draw.rectangle((14, 12, 10 + swatch, 8 + swatch), fill=tuple(record["rgb"]))
    draw.text((16 + swatch + 10, 12), record["review_id"], font=_font(28), fill=MARK)
    for index, (label, value) in enumerate(rows):
        y = 56 + index * 26
        draw.text((12, y + 2), label, font=_font(15), fill=DIM)
        draw.text((96, y), _wrap(value, 44, 1)[0], font=_font(17), fill=FG)

    panel = Image.new("RGB", (base.width, outline.height + caption.height), BG)
    panel.paste(outline, (0, 0))
    panel.paste(caption, (0, outline.height))
    return panel


def _header(pal: str, page: int, pages: int, total: int, base: Image.Image) -> Image.Image:
    reference = base.resize(
        (520, max(1, round(base.height * 520 / base.width))), Image.LANCZOS
    )
    height = max(reference.height + 24, 210)
    canvas = Image.new("RGB", (PAGE_W, height), BG)
    canvas.paste(reference, (12, 12))
    draw = ImageDraw.Draw(canvas)
    x = 556
    draw.text((x, 16), f"{pal}  \u2014  E6 candidate sheet", font=_font(34), fill=FG)
    draw.text((x, 60), f"page {page} of {pages}   \u2014   {total} measured candidates",
              font=_font(22), fill=DIM)
    draw.text((x, 100), "Pick one candidate as FOCUS and one as AMBIANCE.",
              font=_font(22), fill=KEY)
    draw.text((x, 130), "Record their C-numbers. If no candidate expresses what",
              font=_font(20), fill=DIM)
    draw.text((x, 156), "the frame needs, mark the pool insufficient and say why.",
              font=_font(20), fill=DIM)
    draw.text((x, 186), "Candidates are in inventory order, not ranked order.",
              font=_font(19), fill=DIM)
    return canvas


def render_candidate_sheets(pal: str, images: dict, records: list[dict],
                            masks: dict, out_dir: Path) -> list[str]:
    """Write one blind contact sheet per page; return the filenames written."""
    base = R.display_base(images, PANEL_W)
    dimmed = R._dimmed(base, 0.30)
    layers = {
        record["candidate_id"]: R._mask_layer(masks[record["candidate_id"]], base.size)
        for record in records
    }
    panels = [
        _candidate_panel(base, dimmed, layers[record["candidate_id"]], record)
        for record in records
    ]
    per_page = COLUMNS * ROWS_PER_PAGE
    pages = max(1, -(-len(panels) // per_page))
    written = []
    for page in range(pages):
        chunk = panels[page * per_page:(page + 1) * per_page]
        header = _header(pal, page + 1, pages, len(records), base)
        rows = [chunk[index:index + COLUMNS] for index in range(0, len(chunk), COLUMNS)]
        row_height = max(panel.height for panel in chunk) + GAP
        canvas = Image.new("RGB", (PAGE_W, header.height + row_height * len(rows) + GAP), BG)
        canvas.paste(header, (0, 0))
        for row_index, row in enumerate(rows):
            for column, panel in enumerate(row):
                canvas.paste(
                    panel,
                    (column * (PANEL_W + GAP), header.height + row_index * row_height),
                )
        name = f"02-candidate-sheet-p{page + 1}.jpg"
        canvas.save(out_dir / name, format="JPEG", quality=88)
        written.append(name)
    return written
