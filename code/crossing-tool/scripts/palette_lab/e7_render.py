"""Relational inspection sheets for E7.

One sheet per hypothesis, showing the top ordered pairs as relations: the
figure painted in its measured colour, the ground in its own, then the two
together, with the formal/spatial/semantic evidence printed beside them.

The human reference never appears here. Keeping the algorithmic ranking sheet
free of it is what makes the ranking auditable.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from scripts.palette_lab import render as R
from scripts.palette_lab.e6_render import _contour


BG = (16, 16, 18)
FG = (238, 238, 238)
DIM = (152, 152, 160)
FIGURE = (245, 92, 72)
GROUND = (78, 188, 175)

PANEL_W = 380
GAP = 10
TEXT_W = 560
ROW_W = PANEL_W * 3 + GAP * 2 + TEXT_W + GAP


def _font(size: int):
    return R._font(size)


def _paint(base: Image.Image, dimmed: Image.Image, mask, rgb, label: str,
           tint) -> Image.Image:
    layer = R._mask_layer(mask, base.size)
    colour = Image.new("RGB", base.size, tuple(rgb))
    painted = Image.composite(Image.blend(base, colour, 0.74), dimmed, layer)
    painted.paste(Image.new("RGB", base.size, (255, 255, 255)), (0, 0), _contour(layer))
    draw = ImageDraw.Draw(painted)
    draw.rectangle((10, 10, 44, 44), fill=(240, 240, 240))
    draw.rectangle((12, 12, 42, 42), fill=tuple(rgb))
    draw.text((52, 10), label, font=_font(22), fill=tint)
    return painted


def _relation(base: Image.Image, dimmed: Image.Image, figure_mask, ground_mask) -> Image.Image:
    figure_layer = R._mask_layer(figure_mask, base.size)
    ground_layer = R._mask_layer(ground_mask, base.size)
    composed = Image.composite(
        Image.blend(base, Image.new("RGB", base.size, GROUND), 0.58), dimmed, ground_layer
    )
    composed = Image.composite(
        Image.blend(base, Image.new("RGB", base.size, FIGURE), 0.66), composed, figure_layer
    )
    draw = ImageDraw.Draw(composed)
    draw.rectangle((10, 10, 30, 30), fill=FIGURE)
    draw.text((38, 8), "figure", font=_font(19), fill=FG)
    draw.rectangle((10, 38, 30, 58), fill=GROUND)
    draw.text((38, 36), "ground", font=_font(19), fill=FG)
    return composed


def _evidence_text(pair: dict, figure: dict, ground: dict, rank: int,
                   hypothesis: str, height: int) -> Image.Image:
    canvas = Image.new("RGB", (TEXT_W, height), BG)
    draw = ImageDraw.Draw(canvas)
    formal, spatial, semantic = pair["formal"], pair["spatial"], pair["semantic"]
    families = pair["families"]
    hue = formal["hue_relation"]
    rows = [
        ("", f"#{pair.get('rank', rank)}   score {pair['hypotheses'][hypothesis]}"
             + (f"   x{pair['represents']} duplicate records"
                if pair.get("represents", 1) > 1 else "")),
        ("figure", f"{figure['hex']}  {semantic['figure_label'][:30]}"),
        ("ground", f"{ground['hex']}  {semantic['ground_label'][:30]}"),
        ("family", f"formal {families['formal']['score']}   spatial "
                   f"{families['spatial']['score']}   semantic {families['semantic']['score']}"),
        ("formal", f"dE {formal['delta_e']}   dL {formal['delta_lightness']}   "
                   f"dC {formal['delta_chroma']}"),
        ("local", f"local dE {formal['local_delta_e']} over "
                  f"{formal['local_sample_pixels']} px"),
        ("hue", f"{hue['hue_difference_degrees']} deg apart"
                if hue["available"] else "near-neutral; undefined"),
        ("spatial", f"union {spatial['union_coverage']}   overlap IoU "
                    f"{spatial['overlap_iou']}"),
        ("enclosure", f"{spatial['enclosure']} of figure ring is ground   "
                      f"(reverse {spatial['reverse_enclosure']})"),
        ("scale", f"ground share {spatial['relative_scale']}   figure compact "
                  f"{spatial['figure_compactness']}"),
        ("contact", f"{spatial['contact_pixels']} px touching"),
        ("lenses", f"figure {semantic['figure_lens_agreement']}   ground "
                   f"{semantic['ground_lens_agreement']}   disjoint "
                   f"{semantic['lens_disjointness']}"),
        ("duplication", f"figure x{pair['multiplicity']['figure_colour_multiplicity']}   "
                        f"ground x{pair['multiplicity']['ground_colour_multiplicity']}  "
                        f"(not scored)"),
    ]
    for index, (label, value) in enumerate(rows):
        y = 12 + index * 26
        if y + 20 > height:
            break
        draw.text((10, y + 2), label, font=_font(15), fill=DIM)
        draw.text((105, y), value[:56], font=_font(17), fill=FG)
    return canvas


def render_relational_sheets(pal: str, source_path: Path, by_id: dict, masks: dict,
                             tops: dict, out_dir: Path) -> list[str]:
    """Write one sheet per hypothesis; return the filenames written."""
    from scripts.palette_lab import stages

    images = stages.prepare_images(source_path)
    base = R.display_base(images, PANEL_W)
    dimmed = R._dimmed(base, 0.30)
    written = []
    for index, (hypothesis, pairs) in enumerate(sorted(tops.items()), start=4):
        rows = []
        for rank, pair in enumerate(pairs, start=1):
            figure = by_id[pair["figure_id"]]
            ground = by_id[pair["ground_id"]]
            figure_mask = masks[pair["figure_id"]]
            ground_mask = masks[pair["ground_id"]]
            panels = [
                _paint(base, dimmed, figure_mask, figure["rgb"], "FIGURE", FIGURE),
                _paint(base, dimmed, ground_mask, ground["rgb"], "GROUND", GROUND),
                _relation(base, dimmed, figure_mask, ground_mask),
            ]
            row = Image.new("RGB", (ROW_W, base.height + GAP), BG)
            x = 0
            for panel in panels:
                row.paste(panel, (x, 0))
                x += PANEL_W + GAP
            row.paste(_evidence_text(pair, figure, ground, rank, hypothesis, base.height), (x, 0))
            rows.append(row)

        header = Image.new("RGB", (ROW_W, 92), BG)
        draw = ImageDraw.Draw(header)
        draw.text((12, 14), f"{pal}  \u2014  {hypothesis}", font=_font(32), fill=FG)
        draw.text((12, 56), "Top relational pairs. Ordered (figure, ground). "
                            "No human reference shown.", font=_font(20), fill=DIM)
        canvas = Image.new("RGB", (ROW_W, header.height + sum(r.height for r in rows) + GAP), BG)
        canvas.paste(header, (0, 0))
        y = header.height
        for row in rows:
            canvas.paste(row, (0, y))
            y += row.height
        name = f"{index:02d}-relational-{hypothesis}.jpg"
        canvas.save(out_dir / name, format="JPEG", quality=87)
        written.append(name)
    return written
