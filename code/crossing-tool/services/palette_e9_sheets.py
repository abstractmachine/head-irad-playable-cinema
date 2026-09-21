"""Descriptive contact sheets for a frozen E9-A run.

Diagnostic records of what was generated. They are produced after a run and
are never an input to changing the generator.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

from data import palette_review as store
from services import palette_e9 as e9

F, B = store.ROLE_FIGURE, store.ROLE_BACKGROUND
CARD_W = 1180
PAD = 14
INK = (232, 232, 232)
DIM = (150, 150, 150)
BG = (24, 24, 26)
RULE = (58, 58, 62)


def _font(size, bold=False):
    from services.palette_review_sheets import _font as base

    return base(size, bold)


def _rgb(colour):
    rgb = (colour or {}).get("rgb")
    return tuple(int(v) for v in rgb) if rgb else None


def _wrap(text, font, width, draw, limit=2):
    from services.palette_review_sheets import _wrap as base

    return base(text, font, width, draw, limit)


def render_card(frame: dict, note: str = ""):
    """One shot: the frame, every E9-A choice, and the readings behind them."""
    from PIL import Image, ImageDraw

    from services.palette_review_sheets import _swatch

    title, body, small, tiny = _font(19, True), _font(15), _font(13), _font(12)
    card = Image.new("RGB", (CARD_W, 620), BG)
    draw = ImageDraw.Draw(card)
    y = PAD
    draw.text((PAD, y), f"{frame.get('index')}  {frame['shot_id']}", font=title, fill=INK)
    draw.text((PAD + 560, y), frame.get("status", ""), font=small, fill=DIM)
    y += 26
    if note:
        for line in _wrap(note, small, CARD_W - 2 * PAD, draw, 2):
            draw.text((PAD, y), line, font=small, fill=(210, 190, 120))
            y += 16
    y += 4

    try:
        image = Image.open(frame["image"]).convert("RGB")
        image.thumbnail((520, 222), Image.LANCZOS)
        card.paste(image, (PAD, y))
        bottom = y + image.height
    except Exception:
        draw.rectangle([PAD, y, PAD + 520, y + 222], fill=(40, 40, 44))
        bottom = y + 222

    left, sw = PAD + 542, 112
    draw.text((left, y - 18), "E9-A PROPOSALS", font=small, fill=DIM)
    x = left
    for key in sorted(frame.get("choices") or {}):
        choice = frame["choices"][key]
        colours = choice.get("colours") or []
        pair = {F: _rgb(colours[0] if colours else None),
                B: _rgb(colours[1] if len(colours) > 1 else None)}
        if choice.get("active") and any(pair.values()):
            _swatch(draw, (x, y, x + sw, y + 76), pair,
                    f"{key} {choice.get('label', '')[:9]}", "offered",
                    font=small, small=tiny)
        else:
            draw.rectangle([x, y, x + sw, y + 76], outline=RULE)
            draw.text((x + 5, y + 32), "unavailable", font=tiny, fill=DIM)
            draw.text((x, y + 79), f"{key} {choice.get('label', '')[:9]}",
                      font=small, fill=DIM)
        x += sw + 8

    ry = y + 120
    for key in sorted(frame.get("choices") or {}):
        choice = frame["choices"][key]
        if choice.get("active") or choice.get("strategy") == "control":
            continue
        reason = choice.get("inactive_reason") or ""
        for line in _wrap(f"{key}: {reason}", tiny, CARD_W - left - PAD, draw, 1):
            draw.text((left, ry), line, font=tiny, fill=DIM)
            ry += 14

    y = max(bottom, ry) + 12
    draw.line([PAD, y, CARD_W - PAD, y], fill=RULE)
    y += 10
    prov = frame.get("provenance") or {}
    figure = (prov.get("figure") or {}).get("primary") or {}
    field = (prov.get("field") or {}).get("primary") or {}
    for label, value in (
        ("figure", figure.get("figure")),
        ("fig. material", figure.get("material")),
        ("field", field.get("field")),
        ("field material", field.get("material")),
    ):
        draw.text((PAD, y), f"{label:>14}: ", font=small, fill=DIM)
        for line in _wrap(value, body, CARD_W - 2 * PAD - 120, draw, 2):
            draw.text((PAD + 120, y), line, font=body, fill=INK)
            y += 18
        y += 1

    repairs = []
    for key, choice in (frame.get("choices") or {}).items():
        for role in ("figure_material", "field_material"):
            resolved = ((choice.get("e9") or {}).get(role)) or {}
            for item in resolved.get("repairs") or []:
                repairs.append(f"{role.split('_')[0]}: \u201c{item.get('rejected', '')}\u201d "
                               f"\u2192 {item.get('repaired') or item.get('status')}")
    for line in dict.fromkeys(repairs):
        for part in _wrap(line, tiny, CARD_W - 2 * PAD, draw, 1):
            draw.text((PAD, y), part, font=tiny, fill=(190, 170, 120))
            y += 14

    return card.crop((0, 0, CARD_W, min(620, y + PAD)))


def render_sheet(frames: list, notes: dict, path: Path, title: str) -> Path:
    from PIL import Image, ImageDraw

    cards = [render_card(f, notes.get(f["shot_id"], "")) for f in frames]
    if not cards:
        return path
    height = 52 + sum(c.height + 8 for c in cards) + PAD
    sheet = Image.new("RGB", (CARD_W, height), (16, 16, 18))
    draw = ImageDraw.Draw(sheet)
    draw.text((PAD, 16), title, font=_font(22, True), fill=INK)
    y = 52
    for card in cards:
        sheet.paste(card, (0, y))
        y += card.height
        draw.line([0, y + 3, CARD_W, y + 3], fill=RULE)
        y += 8
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, quality=92)
    return path


def load_frames(root: Path) -> list:
    out = []
    for path in sorted((root / "frames").glob("*.json")):
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


def summarise(frames: list) -> dict:
    """Descriptive only: what the frozen run produced."""
    generated = [f for f in frames if f.get("status") == "generated"]
    availability = Counter()
    per_choice = Counter()
    unavailable = {}
    segmentation = Counter()
    repairs = Counter()
    repaired_examples = []
    compound_seen = compound_total = 0
    for frame in generated:
        keys = store.selectable_choices({"proposals": frame.get("choices") or {}})
        availability[len(keys)] += 1
        for key in keys:
            per_choice[key] += 1
        for key, choice in (frame.get("choices") or {}).items():
            if not choice.get("active"):
                unavailable.setdefault(key, Counter())[
                    str(choice.get("inactive_reason"))] += 1
            for record in choice.get("measurement") or []:
                if record:
                    segmentation[str((record.get("segmentation") or {}).get("status"))] += 1
            for material in choice.get("materials") or []:
                compound_total += 1
                if e9.material_problem(material):
                    compound_seen += 1
            for role in ("figure_material", "field_material"):
                resolved = ((choice.get("e9") or {}).get(role)) or {}
                history = resolved.get("repairs") or []
                if history:
                    repairs[resolved.get("status", "?")] += 1
                    if len(repaired_examples) < 25:
                        repaired_examples.append({
                            "shot_id": frame["shot_id"], "role": role,
                            "original": resolved.get("original"),
                            "final": resolved.get("material"),
                            "status": resolved.get("status"),
                        })
    total_seg = sum(segmentation.values())
    return {
        "frames": len(frames),
        "generated": len(generated),
        "failed": len(frames) - len(generated),
        "selectable_choices_per_frame": dict(availability),
        "frames_offering_each_choice": dict(per_choice),
        "unavailable_reasons": {k: dict(v) for k, v in unavailable.items()},
        "segmentation": {
            "counts": dict(segmentation),
            "success_percent": round(100.0 * segmentation.get("success", 0) / total_seg, 1)
            if total_seg else None,
        },
        "material_repairs": dict(repairs),
        "compound_materials_reaching_sam": {
            "materials": compound_total, "flagged": compound_seen,
            "percent": round(100.0 * compound_seen / compound_total, 1)
            if compound_total else None,
        },
        "repair_examples": repaired_examples,
    }


def build(root: Path, *, seed: int = 21) -> list:
    frames = load_frames(root)
    generated = [f for f in frames if f.get("status") == "generated"]
    rng = random.Random(seed)
    out = root / "contact-sheets"
    written = []

    def emit(name, title, picks, note=""):
        picks = list(picks)[:8]
        if picks:
            written.append(str(render_sheet(
                picks, {p["shot_id"]: note for p in picks}, out / name, title)))

    def selectable(frame):
        return store.selectable_choices({"proposals": frame.get("choices") or {}})

    emit("01-primary-available.jpg", "Choice 1 PRIMARY available",
         rng.sample([f for f in generated if "1" in selectable(f)],
                    min(8, len([f for f in generated if "1" in selectable(f)]))),
         "The figure-then-field chain produced a measurable pair.")
    for key, label in (("2", "FIGURE-ALT"), ("3", "FIELD-ALT")):
        pool = [f for f in generated if key in selectable(f)]
        emit(f"0{key}-choice-{key}-available.jpg",
             f"Choice {key} {label} available ({len(pool)} frames)",
             rng.sample(pool, min(8, len(pool))) if pool else [],
             f"A genuinely distinct {label.lower()} was found.")
    emit("04-primary-unavailable.jpg", "Choice 1 unavailable",
         [f for f in generated if "1" not in selectable(f)],
         "The primary chain did not reach a measurable pair.")
    emit("05-material-repairs.jpg", "Compound material repaired before measurement",
         [f for f in generated
          if any((((c.get("e9") or {}).get(r) or {}).get("repairs"))
                 for c in (f.get("choices") or {}).values()
                 for r in ("figure_material", "field_material"))],
         "A compound material was narrowed rather than measured as written.")
    emit("06-segmentation-failures.jpg", "Segmentation reported no usable mask",
         [f for f in generated
          if any((rec or {}).get("segmentation", {}).get("status") in ("no_mask", "error", "unusable")
                 for c in (f.get("choices") or {}).values()
                 for rec in (c.get("measurement") or []))],
         "SAM found nothing for at least one named material.")
    emit("07-control-only.jpg", "Only the frozen control was selectable",
         [f for f in generated if selectable(f) == ["4"]],
         "Every E9-A choice was unavailable on this frame.")
    return written
