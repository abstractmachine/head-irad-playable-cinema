"""Contact sheets and the analysis output tree for a completed palette review.

Renders only. Reads the normalized records and writes beneath
``outputs/tests/palette-review-analysis/<media_id>/``. Nothing here mutates the
review or re-measures anything.
"""

from __future__ import annotations

import json
from pathlib import Path

from data import palette_review as store
from services.palette_review_analysis import OUTPUT_SPACE, SOURCE_MANUAL

ROLES = store.ROLES
FIGURE, BACKGROUND = store.ROLE_FIGURE, store.ROLE_BACKGROUND

CARD_W = 1180
THUMB_W, THUMB_H = 520, 222
PAD = 14
INK = (232, 232, 232)
DIM = (150, 150, 150)
BG = (24, 24, 26)
RULE = (58, 58, 62)


def _font(size: int, bold: bool = False):
    from generators._common import load_font_with_fallback
    from styles import theme

    static = Path(theme.__file__).parent / "fonts" / "Geist" / "static"
    name = "Geist-SemiBold.ttf" if bold else "Geist-Regular.ttf"
    return load_font_with_fallback(size, preferred_paths=[str(static / name)])


def _swatch(draw, box, colour, label, sub=None, font=None, small=None):
    """One palette: background field with the figure disc, plus its provenance."""
    x0, y0, x1, y1 = box
    fg = tuple(colour.get(FIGURE) or (60, 60, 60))
    bg = tuple(colour.get(BACKGROUND) or (40, 40, 40))
    draw.rectangle([x0, y0, x1, y1], fill=bg)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    r = int(min(x1 - x0, y1 - y0) * 0.28)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fg)
    draw.text((x0, y1 + 3), label, font=font, fill=INK)
    if sub:
        draw.text((x0, y1 + 17), sub, font=small, fill=DIM)


def _wrap(text, font, width, draw, limit=3):
    words = str(text or "").split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) == limit:
                break
    if current and len(lines) < limit:
        lines.append(current)
    if len(lines) == limit and len(words) > sum(len(l.split()) for l in lines):
        lines[-1] = lines[-1] + "\u2026"
    return lines


def _rgb(record) -> tuple | None:
    rgb = (record or {}).get("rgb")
    return tuple(int(v) for v in rgb) if rgb else None


def render_card(row: dict, note: str = "") -> "Image.Image":
    """One shot: frame, the generated alternatives, the human result, the reading."""
    from PIL import Image, ImageDraw

    title = _font(19, bold=True)
    body = _font(15)
    small = _font(13)
    tiny = _font(12)

    height = 560
    card = Image.new("RGB", (CARD_W, height), BG)
    draw = ImageDraw.Draw(card)
    y = PAD

    draw.text((PAD, y), f"{row['index']:>4}  {row['shot_id']}", font=title, fill=INK)
    draw.text((PAD + 540, y), f"scene {row['scene']}   state: {row['state']}",
              font=small, fill=DIM)
    y += 26
    if note:
        for line in _wrap(note, small, CARD_W - 2 * PAD, draw, limit=2):
            draw.text((PAD, y), line, font=small, fill=(210, 190, 120))
            y += 16
    y += 4

    try:
        frame = Image.open(row["frame_image"]).convert("RGB")
        frame.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
        card.paste(frame, (PAD, y))
        frame_bottom = y + frame.height
    except Exception:
        draw.rectangle([PAD, y, PAD + THUMB_W, y + THUMB_H], fill=(40, 40, 44))
        frame_bottom = y + THUMB_H

    # ---- generated alternatives + the human result, right of the frame
    left = PAD + THUMB_W + 22
    sw = 108
    sy = y
    draw.text((left, sy - 18), "GENERATED", font=small, fill=DIM)
    x = left
    for key in sorted(row["proposals"]):
        proposal = row["proposals"][key]
        colours = {role: _rgb((proposal["roles"][role] or {}).get("colour"))
                   for role in ROLES}
        if not any(colours.values()):
            draw.rectangle([x, sy, x + sw, sy + 74], outline=RULE)
            draw.text((x + 4, sy + 30), "none", font=tiny, fill=DIM)
        else:
            _swatch(draw, (x, sy, x + sw, sy + 74), colours,
                    f"{key} {proposal['label'][:9]}",
                    "offered" if proposal["active"] else "greyed",
                    font=small, small=tiny)
        x += sw + 10

    sy += 118
    draw.text((left, sy - 18), "DOUGLAS", font=small, fill=DIM)
    final = {role: _rgb(row["roles"].get(role)) for role in ROLES}
    _swatch(draw, (left, sy, left + sw, sy + 74), final, "final", None,
            font=small, small=tiny)

    prov = []
    for role, name in ((FIGURE, "figure"), (BACKGROUND, "backgd")):
        rec = row["roles"].get(role) or {}
        if rec.get("source") == SOURCE_MANUAL:
            sup = rec.get("supersedes")
            tag = f"pipette (over {sup['choice']})" if sup else "pipette"
        else:
            tag = f"proposal {rec.get('choice')} {rec.get('strategy') or ''}"
        prov.append(f"{name}: {rec.get('hex') or '-'}  {tag}")
    py = sy
    for line in prov:
        draw.text((left + sw + 16, py), line, font=small, fill=INK)
        py += 19

    # ---- the reading underneath
    y = max(frame_bottom, sy + 92) + 14
    draw.line([PAD, y, CARD_W - PAD, y], fill=RULE)
    y += 10
    reading = row["narratology"]
    for label, value in (
        ("stands out", reading.get("stands_out")),
        ("against", reading.get("against")),
        ("cue", reading.get("cue")),
    ):
        draw.text((PAD, y), f"{label:>11}: ", font=small, fill=DIM)
        for line in _wrap(value, body, CARD_W - 2 * PAD - 100, draw, limit=2):
            draw.text((PAD + 100, y), line, font=body, fill=INK)
            y += 18
        y += 2

    materials = []
    for key in sorted(row["proposals"]):
        proposal = row["proposals"][key]
        if proposal["strategy"] in ("control", "no_adequate_two_colour"):
            continue
        names = [(proposal["roles"][r] or {}).get("material") for r in ROLES]
        names = [n for n in names if n]
        if names:
            seg = [(proposal["roles"][r] or {}).get("segmentation_status") for r in ROLES]
            materials.append(f"{key} {proposal['label'][:6]}: "
                             f"{' | '.join(names)}  [{', '.join(str(s) for s in seg)}]")
    for line in materials:
        for part in _wrap(line, tiny, CARD_W - 2 * PAD, draw, limit=1):
            draw.text((PAD, y), part, font=tiny, fill=DIM)
            y += 15

    return card.crop((0, 0, CARD_W, min(height, y + PAD)))


def render_sheet(rows: list, notes: dict, path: Path, title: str) -> Path:
    """A vertical stack of cards, one per selected shot."""
    from PIL import Image, ImageDraw

    cards = [render_card(row, notes.get(row["shot_id"], "")) for row in rows]
    header = 52
    width = CARD_W
    height = header + sum(c.height + 8 for c in cards) + PAD
    sheet = Image.new("RGB", (width, height), (16, 16, 18))
    draw = ImageDraw.Draw(sheet)
    draw.text((PAD, 16), title, font=_font(22, bold=True), fill=INK)
    y = header
    for card in cards:
        sheet.paste(card, (0, y))
        y += card.height
        draw.line([0, y + 3, width, y + 3], fill=RULE)
        y += 8
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, quality=92)
    return path


def analysis_root(project_path: str, media_id: str) -> Path:
    return (Path(project_path) / "outputs" / "tests" / OUTPUT_SPACE / media_id)


def write_json(path: Path, payload: dict) -> Path:
    from data.annotate import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False,
                                       default=str), encoding="utf-8")
    return path
