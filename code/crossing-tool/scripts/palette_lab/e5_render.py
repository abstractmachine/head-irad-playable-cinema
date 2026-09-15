"""Inspection rendering for the E5 two-colour reduction experiment."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from scripts.palette_lab import render as R


BG = (16, 16, 18)
FG = (238, 238, 238)
DIM = (152, 152, 160)
FOCUS = (245, 92, 72)
AMBIANCE = (78, 188, 175)


def _font(size: int):
    return R._font(size)


def _mask_panel(images: dict, candidate: dict, tint: tuple[int, int, int],
                title: str, width: int) -> Image.Image:
    base = R.display_base(images, width)
    mask = R._mask_layer(candidate["_mask"], base.size)
    dimmed = R._dimmed(base, 0.38)
    colour = Image.new("RGB", base.size, tint)
    painted = Image.composite(Image.blend(base, colour, 0.70), dimmed, mask)
    draw = ImageDraw.Draw(painted)
    swatch = max(58, width // 9)
    draw.rectangle((12, 12, 12 + swatch, 12 + swatch), fill=(245, 245, 245))
    draw.rectangle((16, 16, 8 + swatch, 8 + swatch), fill=tuple(candidate["rgb"]))
    draw.text((24 + swatch, 14), title, font=_font(24), fill=FG)
    draw.text((24 + swatch, 46), candidate["hex"], font=_font(21), fill=tint)
    return painted


def _pair_panel(images: dict, focus: dict, ambiance: dict, width: int) -> Image.Image:
    base = R.display_base(images, width)
    focus_mask = R._mask_layer(focus["_mask"], base.size)
    ambiance_mask = R._mask_layer(ambiance["_mask"], base.size)
    dimmed = R._dimmed(base, 0.32)
    ambiance_tint = Image.new("RGB", base.size, AMBIANCE)
    focus_tint = Image.new("RGB", base.size, FOCUS)
    composed = Image.composite(Image.blend(base, ambiance_tint, 0.58), dimmed, ambiance_mask)
    composed = Image.composite(Image.blend(base, focus_tint, 0.66), composed, focus_mask)
    draw = ImageDraw.Draw(composed)
    draw.text((14, 14), "JOINT PAIR", font=_font(24), fill=FG)
    draw.rectangle((14, 52, 34, 72), fill=FOCUS)
    draw.text((43, 50), "focus", font=_font(18), fill=FG)
    draw.rectangle((14, 80, 34, 100), fill=AMBIANCE)
    draw.text((43, 78), "ambiance", font=_font(18), fill=FG)
    return composed


def _caption(width: int, lines: list[tuple[str, str]], title: str) -> Image.Image:
    line_height = 28
    canvas = Image.new("RGB", (width, 56 + line_height * len(lines) + 16), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((16, 14), title, font=_font(24), fill=FG)
    for row, (label, value) in enumerate(lines):
        y = 56 + row * line_height
        draw.text((16, y), label, font=_font(16), fill=DIM)
        draw.text((185, y - 2), value, font=_font(18), fill=FG)
    return canvas


def _stack(top: Image.Image, bottom: Image.Image) -> Image.Image:
    canvas = Image.new("RGB", (top.width, top.height + bottom.height), BG)
    canvas.paste(top, (0, 0))
    canvas.paste(bottom, (0, top.height))
    return canvas


def _row(panels: list[Image.Image], gap: int = 8) -> Image.Image:
    height = max(panel.height for panel in panels)
    width = sum(panel.width for panel in panels) + gap * (len(panels) - 1)
    canvas = Image.new("RGB", (width, height), BG)
    x = 0
    for panel in panels:
        canvas.paste(panel, (x, 0))
        x += panel.width + gap
    return canvas


def _wrap(value: str, limit: int) -> list[str]:
    words = str(value or "").split()
    lines, current = [], ""
    for word in words:
        if current and len(current) + len(word) + 1 > limit:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


def _evidence_panel(reduction: dict, focus: dict, ambiance: dict,
                    candidate_count: int, unavailable_count: int, width: int) -> Image.Image:
    metrics = reduction["pair_metrics"]
    canvas = Image.new("RGB", (width, 310), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((16, 14), "E5 TWO-COLOUR REDUCTION", font=_font(26), fill=FG)
    draw.text((16, 52), "All signals remain evidence, not a correctness claim.", font=_font(18), fill=DIM)
    rows = [
        ("candidate pool", f"{candidate_count} measured / {unavailable_count} retained unavailable"),
        ("focus", f"{focus['candidate_id']}  ({focus['source']})"),
        ("ambiance", f"{ambiance['candidate_id']}  ({ambiance['source']})"),
        ("pair score", str(reduction["pair_score"])),
        ("Delta E", str(metrics["delta_e"])),
        ("spatial IoU", str(metrics["spatial_iou"])),
        ("union coverage", str(metrics["union_coverage"])),
        ("shared roles", ", ".join(metrics["shared_semantic_roles"]) or "none"),
    ]
    for row, (label, value) in enumerate(rows):
        y = 86 + row * 27
        draw.text((16, y), label, font=_font(16), fill=DIM)
        draw.text((195, y - 2), value[:75], font=_font(18), fill=FG)
    return canvas


def render_reduction(pal: str, images: dict, reduction: dict, candidates: list[dict],
                     unavailable: list[dict], path: Path) -> None:
    """Render the selected pair in their actual measured colours and pixels."""
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    focus = by_id[reduction["roles"]["focus"]["candidate_id"]]
    ambiance = by_id[reduction["roles"]["ambiance"]["candidate_id"]]
    panel_width = 600
    focus_panel = _stack(
        _mask_panel(images, focus, FOCUS, "FOCUS", panel_width),
        _caption(panel_width, [
            ("colour", f"{focus['hex']}  rgb {focus['rgb']}"),
            ("source", focus["source"]),
            ("coverage", str(focus["coverage"])),
            ("focus evidence", str(focus["ranking"]["focus"]["score"])),
        ], "Measured candidate"),
    )
    ambiance_panel = _stack(
        _mask_panel(images, ambiance, AMBIANCE, "AMBIANCE", panel_width),
        _caption(panel_width, [
            ("colour", f"{ambiance['hex']}  rgb {ambiance['rgb']}"),
            ("source", ambiance["source"]),
            ("coverage", str(ambiance["coverage"])),
            ("ambiance evidence", str(ambiance["ranking"]["ambiance"]["score"])),
        ], "Measured candidate"),
    )
    pair_panel = _stack(
        _pair_panel(images, focus, ambiance, panel_width),
        _caption(panel_width, [
            ("focus", focus["hex"]),
            ("ambiance", ambiance["hex"]),
            ("Delta E", str(reduction["pair_metrics"]["delta_e"])),
            ("spatial IoU", str(reduction["pair_metrics"]["spatial_iou"])),
        ], "Joint evidence"),
    )
    header = Image.new("RGB", (1816, 62), BG)
    ImageDraw.Draw(header).text(
        (12, 16), f"{pal}  E5 focus + ambiance", font=_font(29), fill=FG
    )
    explanation = _evidence_panel(reduction, focus, ambiance, len(candidates), len(unavailable), 1816)
    role_lines = []
    for role, candidate in (("focus", focus), ("ambiance", ambiance)):
        if candidate["source"] == "semantic":
            text = candidate["provenance"].get("interpretation", "")
            role_lines.extend([(role, line) for line in _wrap(text, 150)])
        else:
            support = candidate.get("cross_source", {}).get("semantic_role_support", {})
            role_lines.append((role, "hierarchy region; semantic support " + (str(support) if support else "none")))
    notes = _caption(1816, role_lines, "Frozen E4 reading context")
    sections = [header, _row([focus_panel, ambiance_panel, pair_panel]), explanation, notes]
    canvas = Image.new("RGB", (1816, sum(section.height for section in sections) + 24), BG)
    y = 0
    for section in sections:
        canvas.paste(section, (0, y))
        y += section.height + 8
    canvas.convert("RGB").save(path, format="JPEG", quality=90)