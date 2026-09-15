"""Review sheets for the E8 measured palette experiment.

The sheet is the primary artifact: Douglas should be able to judge by looking.
Measured colour dominates, mask evidence is secondary, and nothing here is
allowed to invent a swatch — a refusal quadrant stays empty on purpose.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from generators._common import load_font_with_fallback

SHEET_W = 1680
MARGIN = 28
CELL_W = (SHEET_W - MARGIN * 3) // 2
CELL_H = 430
SOURCE_H = 470
HEADER_H = 74
FOOTER_H = 96

BG = (16, 16, 18)
PANEL = (26, 26, 30)
FG = (238, 238, 240)
DIM = (150, 150, 156)
FAINT = (92, 92, 100)
RULE = (54, 54, 60)
FLAG = (226, 148, 60)

STATUS_COLOURS = {
    "success": (110, 190, 120),
    "multiple_masks": (226, 196, 90),
    "suspicious_broad": (226, 148, 60),
    "no_mask": (216, 92, 84),
    "error": (216, 92, 84),
    "unusable": (216, 92, 84),
}


def _font(size: int):
    return load_font_with_fallback(size)


def _leading(font, size: int) -> int:
    return int(getattr(font, "size", size)) + 4


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words = str(text or "").split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _text_block(draw, xy, text, font, fill, width, *, size=16, max_lines=None):
    x, y = xy
    lines = _wrap(draw, text, font, width)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,") + "\u2026"
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += _leading(font, size)
    return y


def _overlay(images: dict, mask, size: tuple[int, int]) -> Image.Image:
    """Source thumbnail with the measured pixels kept and the rest dimmed."""
    base = images["active"].resize(size, Image.LANCZOS).convert("RGB")
    dark = Image.blend(Image.new("RGB", size, BG), base, 0.22)
    if mask is None or not np.asarray(mask).any():
        return dark
    layer = Image.fromarray(np.asarray(mask).astype(np.uint8) * 255, mode="L")
    return Image.composite(base, dark, layer.resize(size, Image.Resampling.NEAREST))


def save_masks(images: dict, choices: dict, measured: dict, out_dir: Path) -> None:
    """Persist every union mask that was measured, for later inspection."""
    from scripts.palette_lab.e8_palette_review import normalise_material

    mask_dir = out_dir / "03-masks"
    mask_dir.mkdir(parents=True, exist_ok=True)
    written = set()
    for key in sorted(choices, key=int):
        for index, material in enumerate(choices[key].get("materials") or []):
            found = measured.get(normalise_material(material))
            if found is None:
                continue
            name = f"choice{key}-{'ab'[index] if index < 2 else index}"
            if name in written:
                continue
            written.add(name)
            mask = np.asarray(found["mask"])
            Image.fromarray(mask.astype(np.uint8) * 255, mode="L").save(
                mask_dir / f"{name}.png"
            )
            _overlay(images, mask, (480, 480)).save(
                mask_dir / f"{name}-overlay.jpg", quality=90
            )


def _swatch_pair(canvas, draw, origin, choice, images, measured, width) -> int:
    """Two large measured colours, their materials, and small mask evidence."""
    from scripts.palette_lab.e8_palette_review import normalise_material

    x, y = origin
    colours = choice.get("colours") or []
    materials = choice.get("materials") or []
    records = choice.get("measurement") or []
    gap = 18
    swatch_w = (width - gap) // 2
    swatch_h = 132

    for index in range(2):
        left = x + index * (swatch_w + gap)
        colour = colours[index] if index < len(colours) else None
        box = [left, y, left + swatch_w, y + swatch_h]
        if colour:
            draw.rectangle(box, fill=tuple(colour["rgb"]))
        else:
            draw.rectangle(box, outline=FAINT, width=2)
            draw.text((left + 14, y + swatch_h // 2 - 9), "not measured",
                      font=_font(15), fill=FAINT)

    label_y = y + swatch_h + 10
    bottom = label_y
    for index in range(2):
        left = x + index * (swatch_w + gap)
        material = materials[index] if index < len(materials) else ""
        colour = colours[index] if index < len(colours) else None
        end = _text_block(draw, (left, label_y), material, _font(16), FG,
                          swatch_w, size=16, max_lines=2)
        if colour:
            draw.text((left, end + 2), colour["hex"], font=_font(13), fill=DIM)
        record = records[index] if index < len(records) else None
        status = ((record or {}).get("segmentation") or {}).get("status")
        if status:
            draw.text((left + 78, end + 2), status.replace("_", " "),
                      font=_font(13), fill=STATUS_COLOURS.get(status, DIM))
        bottom = max(bottom, end + 22)

    thumb_w, thumb_h = 132, 84
    for index in range(2):
        left = x + index * (swatch_w + gap)
        material = materials[index] if index < len(materials) else None
        found = measured.get(normalise_material(material)) if material else None
        if found is None:
            draw.text((left, bottom + 4), "whole-frame reduction",
                      font=_font(13), fill=FAINT)
            draw.text((left, bottom + 22), "no mask", font=_font(13), fill=FAINT)
            continue
        canvas.paste(_overlay(images, found["mask"], (thumb_w, thumb_h)),
                     (left, bottom))
    return bottom + thumb_h


def _cell(images, choice, key, measured) -> Image.Image:
    cell = Image.new("RGB", (CELL_W, CELL_H), PANEL)
    draw = ImageDraw.Draw(cell)
    inner = CELL_W - 40
    x, y = 20, 16

    number_fill = FG if choice.get("active") else FAINT
    draw.text((x, y), key, font=_font(34), fill=number_fill)
    draw.text((x + 36, y + 12), choice["label"], font=_font(20), fill=number_fill)
    y += 52
    draw.line([(x, y), (x + inner, y)], fill=RULE, width=1)
    y += 16

    if choice.get("converges_with"):
        origin = ", ".join(choice["converges_with"])
        draw.text((x, y), "CONVERGENCE", font=_font(24), fill=FLAG)
        y += 36
        y = _text_block(
            draw, (x, y),
            f"Same source materials as choice {origin}. Collapsed rather than "
            f"shown twice, so this quadrant is not separately selectable.",
            _font(16), DIM, inner, size=16,
        )
        y += 10
        for material in choice.get("materials") or []:
            y = _text_block(draw, (x, y), f"\u2022 {material}", _font(15), FAINT,
                            inner, size=15, max_lines=2)
        return cell

    if choice["strategy"] == "no_adequate_two_colour":
        for line in ("NO ADEQUATE", "TWO-COLOUR", "PALETTE"):
            draw.text((x, y), line, font=_font(30), fill=FG)
            y += 38
        y += 12
        y = _text_block(draw, (x, y), choice.get("source_note", ""), _font(15),
                        DIM, inner, size=15)
        support = choice.get("frozen_support") or {}
        y += 8
        draw.text((x, y), "frozen verdicts for this frame", font=_font(13), fill=FAINT)
        draw.text((x, y + 20),
                  f"carrier-v1 {support.get('carrier_v1_strength')}"
                  f"   \u00b7   articulation-v1 {support.get('articulation_v1_strength')}",
                  font=_font(15), fill=DIM)
        return cell

    bottom = _swatch_pair(cell, draw, (x, y), choice, images, measured, inner)
    note = (choice.get("method") if choice["strategy"] == "control"
            else choice.get("source_note"))
    _text_block(draw, (x, min(bottom + 10, CELL_H - 36)), note or "", _font(13),
                FAINT, inner, size=13, max_lines=2)
    return cell


def render_sheet(pal: str, entry: dict, images: dict, narratology: dict,
                 choices: dict, measured: dict, path: Path) -> Path:
    height = HEADER_H + SOURCE_H + CELL_H * 2 + MARGIN * 4 + FOOTER_H
    sheet = Image.new("RGB", (SHEET_W, height), BG)
    draw = ImageDraw.Draw(sheet)

    draw.text((MARGIN, 22), pal, font=_font(38), fill=FG)
    draw.text((MARGIN + 150, 36), f"{entry.get('film', '')}  {entry.get('year', '')}",
              font=_font(19), fill=DIM)
    draw.text((SHEET_W - MARGIN - 340, 36),
              "measured narratological palette review", font=_font(16), fill=FAINT)

    top = HEADER_H
    source = images["active"]
    source_w = min(int(source.width * SOURCE_H / source.height),
                   SHEET_W - MARGIN * 2 - 560)
    sheet.paste(source.resize((source_w, SOURCE_H), Image.LANCZOS), (MARGIN, top))

    text_x = MARGIN + source_w + 32
    text_w = SHEET_W - text_x - MARGIN
    y = top + 6
    stands_out = (narratology.get("what_stands_out") or [{}])[0]
    cue = narratology.get("strongest_cue") or {}
    for title, value in (
        ("WHAT STANDS OUT", stands_out.get("entity")),
        ("AGAINST", stands_out.get("against")),
        ("CUE", cue.get("visible_cue")),
    ):
        draw.text((text_x, y), title, font=_font(14), fill=FAINT)
        y = _text_block(draw, (text_x, y + 22), value, _font(21), FG, text_w,
                        size=21, max_lines=3)
        y += 22

    grid_top = top + SOURCE_H + MARGIN
    for index, key in enumerate(sorted(choices, key=int)):
        column, row = index % 2, index // 2
        sheet.paste(
            _cell(images, choices[key], key, measured),
            (MARGIN + column * (CELL_W + MARGIN), grid_top + row * (CELL_H + MARGIN)),
        )

    footer_y = grid_top + CELL_H * 2 + MARGIN + 18
    draw.text((MARGIN, footer_y), "CHOOSE", font=_font(26), fill=DIM)
    x = MARGIN + 150
    for key in sorted(choices, key=int):
        draw.text((x, footer_y - 6), key, font=_font(38),
                  fill=FG if choices[key].get("active") else FAINT)
        x += 62
    sheet.save(path, quality=94)
    return path


def render_contact_sheet(manifests: list[dict], out_root: Path) -> Path:
    """One small cross-image sheet: every frame beside every measured pair."""
    row_h, thumb_w = 196, 300
    swatch_w, swatch_h = 116, 58
    width = MARGIN * 2 + thumb_w + 24 + 4 * (swatch_w * 2 + 40)
    sheet = Image.new("RGB", (width, HEADER_H + row_h * len(manifests) + MARGIN), BG)
    draw = ImageDraw.Draw(sheet)
    draw.text((MARGIN, 22), "E8 measured narratological palettes",
              font=_font(30), fill=FG)
    draw.text((MARGIN, 56),
              "four calibration frames \u00b7 up to four hypotheses each",
              font=_font(15), fill=DIM)

    for row, manifest in enumerate(manifests):
        top = HEADER_H + row * row_h
        pal = manifest["id"]
        thumb = Image.open(out_root / pal / "01-source.jpg").convert("RGB")
        scale = (row_h - 46) / thumb.height
        thumb = thumb.resize(
            (min(thumb_w, int(thumb.width * scale)), row_h - 46), Image.LANCZOS
        )
        sheet.paste(thumb, (MARGIN, top + 24))
        draw.text((MARGIN, top + 4), pal, font=_font(17), fill=FG)

        x = MARGIN + thumb_w + 24
        for key in sorted(manifest["choices"], key=int):
            choice = manifest["choices"][key]
            draw.text((x, top + 4), f"{key} {choice['label']}", font=_font(13),
                      fill=FG if choice.get("active") else FAINT)
            colours = [item for item in (choice.get("colours") or []) if item]
            if choice.get("converges_with"):
                draw.text((x, top + 34), f"= choice {choice['converges_with'][0]}",
                          font=_font(15), fill=FLAG)
                colours = []
            elif not colours:
                draw.text((x, top + 34), "no palette", font=_font(15), fill=FAINT)
            for index, colour in enumerate(colours[:2]):
                left = x + index * (swatch_w + 6)
                draw.rectangle(
                    [left, top + 28, left + swatch_w, top + 28 + swatch_h],
                    fill=tuple(colour["rgb"]),
                )
                draw.text((left, top + 32 + swatch_h), colour["hex"],
                          font=_font(12), fill=DIM)
            x += swatch_w * 2 + 40

    path = out_root / "contact-sheet.jpg"
    sheet.save(path, quality=94)
    return path
