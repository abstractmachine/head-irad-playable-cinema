"""Visual artifact rendering for the palette research laboratory.

Every stage of the experiment must be inspectable by opening a PNG.  These
renderers are deliberately literal: candidates are painted in their own
measured colour, never in an arbitrary palette, so that looking at a panel
answers "what is candidate 3?" directly.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from generators._common import load_font_with_fallback

DISPLAY_WIDTH = 1100
PANEL_WIDTH = 520
CAPTION_BG = (18, 18, 20)
CAPTION_FG = (235, 235, 235)
CAPTION_DIM = (150, 150, 155)

OWNERSHIP_COLORS = {
    "focus": (235, 70, 70),
    "ambience": (55, 115, 235),
    "residual": (120, 120, 120),
}


def _font(size: int):
    return load_font_with_fallback(size)


def display_base(images: dict, width: int = DISPLAY_WIDTH) -> Image.Image:
    active = images["active"]
    height = max(1, round(active.height * width / active.width))
    return active.resize((width, height), Image.LANCZOS)


def _mask_layer(mask: np.ndarray, size: tuple[int, int]) -> Image.Image:
    layer = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
    return layer.resize(size, Image.Resampling.NEAREST)


def _dimmed(base: Image.Image, amount: float = 0.32) -> Image.Image:
    grey = base.convert("L").convert("RGB")
    return Image.blend(Image.new("RGB", base.size, (0, 0, 0)), grey, amount)


def _caption_block(width: int, lines: list[tuple[str, str]], *, title: str = "") -> Image.Image:
    title_font = _font(26)
    key_font = _font(18)
    value_font = _font(20)
    line_height = 30
    top = 54 if title else 18
    height = top + line_height * len(lines) + 18
    block = Image.new("RGB", (width, height), CAPTION_BG)
    draw = ImageDraw.Draw(block)
    if title:
        draw.text((24, 16), title, font=title_font, fill=CAPTION_FG)
    for row, (key, value) in enumerate(lines):
        y = top + row * line_height
        draw.text((24, y), key, font=key_font, fill=CAPTION_DIM)
        draw.text((250, y - 2), value, font=value_font, fill=CAPTION_FG)
    return block


def _stack(images: list[Image.Image]) -> Image.Image:
    width = max(image.width for image in images)
    height = sum(image.height for image in images)
    canvas = Image.new("RGB", (width, height), CAPTION_BG)
    y = 0
    for image in images:
        canvas.paste(image, (0, y))
        y += image.height
    return canvas


def _wrap(text: str, limit: int = 62) -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > limit:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


# ---------------------------------------------------------------------------
# Stage artifacts
# ---------------------------------------------------------------------------

def save_source(images: dict, path: Path) -> None:
    images["source"].convert("RGB").save(path, format="JPEG", quality=92)


def render_interpretation(images: dict, focus: dict, path: Path) -> None:
    """Source image plus the model's stated reading of it."""
    base = display_base(images)
    parsed = focus.get("parsed") or {}
    focus_block = parsed.get("focus") or {}
    ambience_block = parsed.get("ambience") or {}
    alternative = parsed.get("alternative_focus") or {}

    lines = [
        ("FOCUS", ""),
        ("  description", str(focus_block.get("description", "—"))[:70]),
        ("  segment phrase", str(focus_block.get("segment_phrase", "—"))),
        ("  fallbacks", ", ".join(focus_block.get("segment_fallbacks") or []) or "—"),
        ("  type / reason", f"{focus_block.get('type', '—')} / {focus_block.get('reason', '—')}"),
        ("  location / extent", f"{focus_block.get('location', '—')} / {focus_block.get('extent', '—')}"),
        ("  confidence", str(focus_block.get("confidence", "—"))),
        ("", ""),
        ("AMBIENCE", ""),
        ("  description", str(ambience_block.get("description", "—"))[:70]),
        ("  segment phrase", str(ambience_block.get("segment_phrase", "—"))),
        ("", ""),
        ("ALTERNATIVE", str(alternative.get("description", "—"))[:70]),
        ("EXPECTED APPEARANCE", str(parsed.get("expected_appearance", "—"))[:70]),
    ]
    caption = _caption_block(base.width, lines, title="Qwen #1 — interpretation")
    _stack([base, caption]).save(path, format="PNG")


def render_masks(images: dict, spatial: dict, path: Path, *, role: str) -> None:
    """Union mask tinted over the source, with per-instance outlines."""
    base = display_base(images)
    colour = OWNERSHIP_COLORS["focus" if role == "focus" else "ambience"]
    union = _mask_layer(spatial["union"], base.size)
    tint = Image.new("RGB", base.size, colour)
    composed = Image.composite(Image.blend(base, tint, 0.5), _dimmed(base, 0.55), union)

    draw = ImageDraw.Draw(composed)
    scale_x = base.width / spatial["union"].shape[1]
    scale_y = base.height / spatial["union"].shape[0]
    label_font = _font(22)
    for order, mask in enumerate(spatial["masks"]):
        x0, y0, x1, y1 = mask["bbox"]
        box = (x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y)
        draw.rectangle(box, outline=(255, 255, 255), width=2)
        draw.text((box[0] + 6, box[1] + 4), str(order), font=label_font, fill=(255, 255, 255))

    attempts = " -> ".join(
        f"{a['phrase']}({a['mask_count']})" for a in spatial["attempts"]
    )
    lines = [
        ("requested", str(spatial.get("requested") or "—")),
        ("accepted", str(spatial.get("accepted_phrase") or "NONE")),
        ("ladder rung", str(spatial.get("ladder_rung"))),
        ("attempts", attempts[:64]),
        ("masks", str(len(spatial["masks"]))),
        ("coverage", f"{float(spatial['union'].mean()):.4f}"),
        ("resolution", str(spatial.get("resolution"))),
    ]
    caption = _caption_block(base.width, lines, title=f"SAM3 — {role} spatialization")
    _stack([composed, caption]).save(path, format="PNG")


def render_seeds(images: dict, labels: np.ndarray, path: Path) -> None:
    boundaries = np.zeros(labels.shape, dtype=bool)
    horizontal = labels[:, 1:] != labels[:, :-1]
    vertical = labels[1:, :] != labels[:-1, :]
    boundaries[:, 1:] |= horizontal
    boundaries[:, :-1] |= horizontal
    boundaries[1:, :] |= vertical
    boundaries[:-1, :] |= vertical

    base = display_base(images)
    layer = _mask_layer(boundaries, base.size)
    composed = Image.composite(Image.new("RGB", base.size, (0, 255, 225)), base, layer)
    caption = _caption_block(
        base.width,
        [("superpixels", str(len(np.unique(labels)))), ("working size", f"{labels.shape[1]}x{labels.shape[0]}")],
        title="SEEDS — spatial decomposition",
    )
    _stack([composed, caption]).save(path, format="PNG")


def render_ownership(images: dict, decomposition: dict, path: Path) -> None:
    labels = decomposition["labels"]
    pixels = np.zeros((*labels.shape, 3), dtype=np.uint8)
    boundary = np.zeros(labels.shape, dtype=bool)
    for record in decomposition["superpixels"]:
        selector = labels == record["label"]
        pixels[selector] = OWNERSHIP_COLORS[record["owner"]]
        if record["boundary"]:
            boundary |= selector

    base = display_base(images)
    tint = Image.fromarray(pixels, mode="RGB").resize(base.size, Image.Resampling.NEAREST)
    composed = Image.blend(base, tint, 0.55)
    hatch = _mask_layer(boundary, base.size)
    composed = Image.composite(Image.blend(composed, Image.new("RGB", base.size, (255, 215, 0)), 0.28), composed, hatch)

    summary = decomposition["summary"]
    lines = [
        ("focus superpixels", f"{summary['focus_superpixels']}  (red)"),
        ("ambience superpixels", f"{summary['ambience_superpixels']}  (blue)"),
        ("residual superpixels", f"{summary['residual_superpixels']}  (grey)"),
        ("mixed superpixels", f"{summary['boundary_superpixels']}  (gold wash)"),
        ("focus pixel coverage", str(summary["focus_pixel_coverage"])),
        ("ambience pixel coverage", str(summary["ambience_pixel_coverage"])),
        ("residual pixel coverage", str(summary["residual_pixel_coverage"])),
        ("focus/ambience overlap", str(summary["focus_ambience_overlap"])),
    ]
    caption = _caption_block(base.width, lines, title="Superpixel ownership")
    _stack([composed, caption]).save(path, format="PNG")


def render_ward_clusters(images: dict, decomposition: dict, measurements: dict, path: Path) -> None:
    """All candidates at once, each painted in its own measured colour."""
    labels = decomposition["labels"]
    pixels = np.zeros((*labels.shape, 3), dtype=np.uint8)
    covered = np.zeros(labels.shape, dtype=bool)
    for region, measurement in measurements.items():
        region_mask = decomposition["masks"][region]
        for candidate in measurement.get("candidates", []):
            selector = np.isin(labels, candidate["superpixel_labels"]) & region_mask
            pixels[selector] = candidate["rgb"]
            covered |= selector

    base = display_base(images)
    tint = Image.fromarray(pixels, mode="RGB").resize(base.size, Image.Resampling.NEAREST)
    layer = _mask_layer(covered, base.size)
    composed = Image.composite(tint, _dimmed(base, 0.4), layer)

    lines = []
    for region, measurement in measurements.items():
        count = len(measurement.get("candidates", []))
        lines.append((f"{region} candidates", str(count) if not measurement.get("error") else measurement["error"][:50]))
    caption = _caption_block(base.width, lines, title="Ward — measured colour families")
    _stack([composed, caption]).save(path, format="PNG")


def render_candidate(
    images: dict,
    labels: np.ndarray,
    region_mask: np.ndarray,
    candidate: dict,
    path: Path,
    *,
    region: str,
    width: int = DISPLAY_WIDTH,
) -> Image.Image:
    """One candidate: its measured pixels, in its measured colour."""
    base = display_base(images, width)
    selector = np.isin(labels, candidate["superpixel_labels"]) & region_mask
    layer = _mask_layer(selector, base.size)
    flat = Image.new("RGB", base.size, tuple(candidate["rgb"]))
    highlighted = Image.blend(base, flat, 0.88)
    composed = Image.composite(highlighted, _dimmed(base), layer)

    swatch_size = max(64, width // 10)
    draw = ImageDraw.Draw(composed)
    draw.rectangle((0, 0, swatch_size + 8, swatch_size + 8), fill=(255, 255, 255))
    draw.rectangle((4, 4, swatch_size + 4, swatch_size + 4), fill=tuple(candidate["rgb"]))
    index_font = _font(max(40, width // 14))
    draw.text((swatch_size + 24, 8), str(candidate["index"]), font=index_font, fill=(255, 255, 255))

    lines = [
        ("region", region),
        ("colour", f"{candidate['hex']}   rgb {candidate['rgb']}"),
        ("lightness / chroma", f"{candidate['lightness']} / {candidate['chroma']}"),
        ("coverage of region", str(candidate["coverage"])),
        ("superpixels", str(candidate["superpixel_count"])),
        ("spread / centre", f"{candidate['spread']} / {candidate['centre']}"),
        ("distance to focus", str(candidate["focus_distance"])),
    ]
    caption = _caption_block(base.width, lines, title=f"Candidate {candidate['index']}")
    result = _stack([composed, caption])
    if path is not None:
        result.save(path, format="PNG")
    return result


def render_candidate_sheet(
    images: dict,
    labels: np.ndarray,
    decomposition: dict,
    measurement: dict,
    path: Path,
    *,
    region: str,
    columns: int = 3,
) -> None:
    """Contact sheet of every candidate for one region."""
    candidates = measurement.get("candidates", [])
    if not candidates:
        base = display_base(images, PANEL_WIDTH)
        caption = _caption_block(
            base.width,
            [("error", str(measurement.get("error"))[:60])],
            title=f"{region} — no candidates",
        )
        _stack([base, caption]).save(path, format="PNG")
        return

    region_mask = decomposition["masks"][region]
    panels = [
        render_candidate(
            images, labels, region_mask, candidate, None,
            region=region, width=PANEL_WIDTH,
        )
        for candidate in candidates
    ]
    cell_w = max(p.width for p in panels)
    cell_h = max(p.height for p in panels)
    rows = (len(panels) + columns - 1) // columns
    header_h = 64
    sheet = Image.new(
        "RGB",
        (cell_w * columns + 8 * (columns + 1), header_h + rows * (cell_h + 8) + 8),
        CAPTION_BG,
    )
    ImageDraw.Draw(sheet).text(
        (16, 18),
        f"{region.upper()} — {len(panels)} measured candidates, each shown in its own measured colour",
        font=_font(26),
        fill=CAPTION_FG,
    )
    for order, panel in enumerate(panels):
        col = order % columns
        row = order // columns
        sheet.paste(panel, (8 + col * (cell_w + 8), header_h + row * (cell_h + 8)))
    sheet.save(path, format="PNG")


def render_curator_sheet(
    images: dict,
    labels: np.ndarray,
    decomposition: dict,
    measurements: dict,
    path: Path,
) -> None:
    """The single image handed to the curator in the visual arm."""
    sections: list[Image.Image] = []
    header = Image.new("RGB", (PANEL_WIDTH * 4 + 40, 70), CAPTION_BG)
    ImageDraw.Draw(header).text(
        (16, 20),
        "Measured candidates — each panel shows that candidate's actual pixels in its own measured colour",
        font=_font(24),
        fill=CAPTION_FG,
    )
    sections.append(header)

    for region, measurement in measurements.items():
        candidates = measurement.get("candidates", [])
        if not candidates:
            continue
        region_mask = decomposition["masks"][region]
        panels = [
            render_candidate(
                images, labels, region_mask, candidate, None,
                region=region, width=PANEL_WIDTH,
            )
            for candidate in candidates
        ]
        cell_w = max(p.width for p in panels)
        cell_h = max(p.height for p in panels)
        columns = min(4, len(panels))
        rows = (len(panels) + columns - 1) // columns
        strip = Image.new(
            "RGB",
            (cell_w * columns + 8 * (columns + 1), 48 + rows * (cell_h + 8)),
            CAPTION_BG,
        )
        ImageDraw.Draw(strip).text((16, 12), region.upper(), font=_font(26), fill=CAPTION_FG)
        for order, panel in enumerate(panels):
            col = order % columns
            row = order // columns
            strip.paste(panel, (8 + col * (cell_w + 8), 48 + row * (cell_h + 8)))
        sections.append(strip)

    _stack(sections).save(path, format="PNG")


def render_selection(
    images: dict,
    labels: np.ndarray,
    decomposition: dict,
    measurements: dict,
    curator: dict,
    path: Path,
) -> None:
    """What the curator chose, over the image, with its stated reasoning."""
    parsed = curator.get("parsed") or {}
    base = display_base(images)
    composed = _dimmed(base, 0.45)
    chosen: dict[str, dict] = {}

    for region, key in (("focus", "focus_index"), ("ambience", "ambience_index")):
        measurement = measurements.get(region, {})
        candidates = measurement.get("candidates", [])
        index = parsed.get(key)
        candidate = next((c for c in candidates if c["index"] == index), None)
        if candidate is None:
            continue
        chosen[region] = candidate
        selector = np.isin(labels, candidate["superpixel_labels"]) & decomposition["masks"][region]
        layer = _mask_layer(selector, base.size)
        flat = Image.new("RGB", base.size, tuple(candidate["rgb"]))
        composed = Image.composite(Image.blend(base, flat, 0.85), composed, layer)

    swatch = 96
    draw = ImageDraw.Draw(composed)
    for order, region in enumerate(("focus", "ambience")):
        candidate = chosen.get(region)
        x = 16 + order * (swatch + 16)
        draw.rectangle((x, 16, x + swatch, 16 + swatch), fill=(255, 255, 255))
        if candidate:
            draw.rectangle((x + 4, 20, x + swatch - 4, 12 + swatch), fill=tuple(candidate["rgb"]))
            draw.text((x + 8, 20 + swatch), f"{region} {candidate['index']}", font=_font(20), fill=(255, 255, 255))

    lines: list[tuple[str, str]] = []
    for region, index_key, is_key, reason_key in (
        ("FOCUS", "focus_index", "focus_is", "focus_reason"),
        ("AMBIENCE", "ambience_index", "ambience_is", "ambience_reason"),
    ):
        lines.append((region, f"candidate {parsed.get(index_key, '—')}"))
        lines.append(("  is", str(parsed.get(is_key, "—"))[:70]))
        for order, chunk in enumerate(_wrap(parsed.get(reason_key, "—"), 70)[:3]):
            lines.append(("  because" if order == 0 else "", chunk))
    if parsed.get("focus_rejected"):
        lines.append(("REJECTED", str(parsed["focus_rejected"])[:70]))
    lines.append(("CONFIDENCE", str(parsed.get("confidence", "—"))))
    for order, chunk in enumerate(_wrap(parsed.get("notes", ""), 70)[:4]):
        lines.append(("NOTES" if order == 0 else "", chunk))

    caption = _caption_block(base.width, lines, title="Qwen #2 — curatorial selection")
    _stack([composed, caption]).save(path, format="PNG")


def render_production_comparison(images: dict, production: dict, path: Path) -> None:
    """Side-by-side swatches: production winner vs. the lab's curated pick."""
    base = display_base(images, 640)
    swatch_h = 140
    strip = Image.new("RGB", (base.width, swatch_h * len(production)), CAPTION_BG)
    draw = ImageDraw.Draw(strip)
    for order, (label, rgb) in enumerate(production.items()):
        y = order * swatch_h
        if rgb:
            draw.rectangle((0, y, base.width // 2, y + swatch_h - 4), fill=tuple(rgb))
        draw.text((base.width // 2 + 20, y + swatch_h // 2 - 12), f"{label}  {rgb}", font=_font(22), fill=CAPTION_FG)
    _stack([base, strip]).save(path, format="PNG")
