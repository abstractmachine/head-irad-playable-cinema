"""E4 pilot — compositional readings laboratory.

Two branches run over the same frozen corpus image and are then compared:

* top-down: Qwen reads the image through seven independent visual lenses, and
  SAM3 tries to spatialize each reading, keeping every instance mask separate;
* bottom-up: SEEDS superpixels are merged only between spatial neighbours into
  12 / 6 / 3 region abstractions painted in their own measured colours.

Nothing is reduced to a palette here.  E3 is not touched.

    uv run python -m scripts.palette_lab.e4_pilot
"""

from __future__ import annotations

import json
import math
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from scripts.palette_lab import contact_sheet as CS
from scripts.palette_lab import corpus as C
from scripts.palette_lab import hierarchy as H
from scripts.palette_lab import prompts as prompt_io
from scripts.palette_lab import render as R
from scripts.palette_lab import stages

SPACE = "palette-system-2026-08-19"
EXPERIMENT = "E4-pilot"
CORPUS_SPACE = "palette-system-2026-08-18"

PILOT = ["PAL-001", "PAL-004", "PAL-005", "PAL-015", "PAL-017",
         "PAL-019", "PAL-020", "PAL-022", "PAL-030"]

ROLES = ["narrative", "compositional", "chromatic", "luminance",
         "iconographic", "atmospheric", "graphic"]

ROLE_COLOR = {
    "narrative":     (235, 90, 80),
    "compositional": (95, 165, 240),
    "chromatic":     (240, 185, 70),
    "luminance":     (225, 225, 225),
    "iconographic":  (170, 120, 225),
    "atmospheric":   (95, 200, 165),
    "graphic":       (245, 130, 200),
}

# Declared 3x3 cell -> normalised centre. "distributed" has no point.
CELLS = {
    "upper_left": (1 / 6, 1 / 6), "upper_center": (0.5, 1 / 6), "upper_right": (5 / 6, 1 / 6),
    "left": (1 / 6, 0.5), "center": (0.5, 0.5), "right": (5 / 6, 0.5),
    "lower_left": (1 / 6, 5 / 6), "lower_center": (0.5, 5 / 6), "lower_right": (5 / 6, 5 / 6),
}

BG = (16, 16, 18)
FG = (238, 238, 238)
DIM = (152, 152, 160)
WORKING = 256


# ---------------------------------------------------------------------------
# Stage 1 — compositional reading
# ---------------------------------------------------------------------------

def read_composition(pipeline, project: str, source: dict, annotation: dict,
                     kind: str, image, max_new_tokens: int) -> dict:
    pair = prompt_io.load_pair(project, "composition")
    variables = stages.focus_variables(source, annotation, kind)
    system = prompt_io.strip_comments(pair["system"])
    user = prompt_io.substitute(prompt_io.strip_comments(pair["user"]), variables)
    result = stages.call_qwen(pipeline, system, user, [image], max_new_tokens)
    result["system_file"] = pair["system_file"]
    result["user_file"] = pair["user_file"]
    result["variables"] = variables
    result["readings"] = _normalise_readings(result.get("parsed"))
    return result


def _normalise_readings(parsed) -> list[dict]:
    """Return one entry per role, whatever shape the model produced."""
    found: dict[str, dict] = {}
    if isinstance(parsed, dict):
        items = parsed.get("readings")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("role") in ROLES:
                    found[item["role"]] = item
        else:
            for role in ROLES:
                if isinstance(parsed.get(role), dict):
                    found[role] = {**parsed[role], "role": role}

    readings = []
    for role in ROLES:
        item = found.get(role) or {}
        phrases = item.get("segment_phrases") or []
        if isinstance(phrases, str):
            phrases = [phrases]
        interpretation = str(item.get("interpretation", "") or "")
        readings.append({
            "role": role,
            "present": bool(found.get(role)),
            "interpretation": interpretation,
            "color_language": str(item.get("color_language", "") or ""),
            "declared_spatializable": bool(item.get("spatializable")),
            "segment_phrases": [str(p).strip() for p in phrases if str(p).strip()],
            "location": str(item.get("location", "") or ""),
            "extent": str(item.get("extent", "") or ""),
            "is_none": interpretation.strip().lower() in ("none", "", "n/a"),
        })
    return readings


# ---------------------------------------------------------------------------
# Stage 2 — spatialization, every mask preserved
# ---------------------------------------------------------------------------

def spatialize_roles(segmenter, images: dict, readings: list[dict]) -> dict:
    """Run every phrase of every spatializable reading; keep all instances."""
    target = images["working"]
    cache: dict[str, list[dict]] = {}
    per_role: list[dict] = []

    for reading in readings:
        record = {
            "role": reading["role"],
            "attempted": [],
            "masks": [],
            "declared_point": CELLS.get(reading["location"]),
            "location": reading["location"],
            "extent": reading["extent"],
        }
        if reading["declared_spatializable"] and reading["segment_phrases"] and not reading["is_none"]:
            for phrase in reading["segment_phrases"]:
                if phrase not in cache:
                    cache[phrase] = _segment(segmenter, target, phrase)
                masks = cache[phrase]
                record["attempted"].append({"phrase": phrase, "mask_count": len(masks)})
                for mask in masks:
                    record["masks"].append({**mask, "phrase": phrase})

        _score_deixis(record)
        per_role.append(record)

    return {"roles": per_role, "phrases_run": sorted(cache)}


def _segment(segmenter, image_pil, phrase: str) -> list[dict]:
    try:
        raw = segmenter.segment_concept(image_pil, phrase)
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}", "mask": None}]

    out = []
    for index, item in enumerate(raw):
        segmentation = np.asarray(item.get("segmentation"), dtype=bool)
        if segmentation.shape != (WORKING, WORKING) or not segmentation.any():
            continue
        rows, cols = np.nonzero(segmentation)
        out.append({
            "mask_index": index,
            "mask": segmentation,
            "score": round(float(item.get("predicted_iou", 0.0)), 4),
            "area": int(segmentation.sum()),
            "relative_area": round(float(segmentation.mean()), 5),
            "centroid": [round(float(cols.mean()) / WORKING, 4),
                         round(float(rows.mean()) / WORKING, 4)],
            "bbox": [int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())],
        })
    return out


def _score_deixis(record: dict) -> None:
    """Measure every mask against Qwen's declared location. Nothing is dropped."""
    point = record["declared_point"]
    for mask in record["masks"]:
        if "centroid" not in mask:
            continue
        if point is None:
            mask["distance_to_declared"] = None
            mask["inside_declared_cell"] = None
            continue
        cx, cy = mask["centroid"]
        mask["distance_to_declared"] = round(math.hypot(cx - point[0], cy - point[1]), 4)
        mask["inside_declared_cell"] = bool(
            abs(cx - point[0]) <= 1 / 6 and abs(cy - point[1]) <= 1 / 6
        )

    usable = [m for m in record["masks"] if m.get("distance_to_declared") is not None]
    record["best_by_location"] = (
        min(usable, key=lambda m: m["distance_to_declared"])["mask_index"] if usable else None
    )
    sized = [m for m in record["masks"] if "area" in m]
    record["largest_mask"] = max(sized, key=lambda m: m["area"])["mask_index"] if sized else None
    record["mask_count"] = len(sized)
    record["deixis_agrees_with_size"] = (
        record["best_by_location"] is not None
        and record["best_by_location"] == record["largest_mask"]
    )


def role_union(record: dict) -> np.ndarray:
    union = np.zeros((WORKING, WORKING), dtype=bool)
    for mask in record["masks"]:
        if isinstance(mask.get("mask"), np.ndarray):
            union |= mask["mask"]
    return union


# ---------------------------------------------------------------------------
# Stage 4 — map readings onto the hierarchy
# ---------------------------------------------------------------------------

def map_roles_to_hierarchy(spatial: dict, hier: dict) -> dict:
    mapping: dict = {}
    for record in spatial["roles"]:
        union = role_union(record)
        entry = {"mask_pixels": int(union.sum()), "levels": {}}
        if union.any():
            for level, data in hier["levels"].items():
                assignment = data["assignment"]
                overlaps = []
                for region in data["regions"]:
                    region_mask = assignment == region["index"]
                    shared = int((region_mask & union).sum())
                    if not shared:
                        continue
                    overlaps.append({
                        "region": region["index"],
                        "hex": region["hex"],
                        "share_of_role": round(shared / int(union.sum()), 3),
                        "share_of_region": round(shared / int(region_mask.sum()), 3),
                    })
                overlaps.sort(key=lambda o: -o["share_of_role"])
                entry["levels"][str(level)] = overlaps[:6]
        mapping[record["role"]] = entry
    return mapping


def semantic_residual(spatial: dict, hier: dict) -> dict:
    covered = np.zeros((WORKING, WORKING), dtype=bool)
    for record in spatial["roles"]:
        covered |= role_union(record)
    residual = ~covered

    levels = {}
    for level, data in hier["levels"].items():
        assignment = data["assignment"]
        rows = []
        for region in data["regions"]:
            region_mask = assignment == region["index"]
            inside = float((region_mask & residual).sum()) / max(1, int(region_mask.sum()))
            rows.append({
                "region": region["index"],
                "hex": region["hex"],
                "coverage": region["coverage"],
                "residual_fraction": round(inside, 3),
            })
        rows.sort(key=lambda r: (-r["residual_fraction"], -r["coverage"]))
        levels[str(level)] = rows
    return {
        "residual_coverage": round(float(residual.mean()), 4),
        "covered_coverage": round(float(covered.mean()), 4),
        "levels": levels,
        "_mask": residual,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _font(size: int):
    return CS._font(size)


def _wrap(text: str, limit: int) -> list[str]:
    words = str(text or "").split()
    lines, current = [], ""
    for word in words:
        if len(current) + len(word) + 1 > limit:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


def _readings_panel(readings: list[dict], width: int, spatial: dict | None = None) -> Image.Image:
    by_role = {r["role"]: r for r in (spatial["roles"] if spatial else [])}
    blocks = []
    for reading in readings:
        lines = _wrap(reading["interpretation"] or "—", max(28, width // 11))
        tail = []
        if reading["color_language"]:
            tail.append(f"colour: {reading['color_language']}")
        phrases = ", ".join(reading["segment_phrases"]) or "—"
        record = by_role.get(reading["role"])
        found = f"{record['mask_count']} masks" if record else ""
        tail.append(
            f"seg[{'Y' if reading['declared_spatializable'] else 'N'}]: {phrases}"
            + (f"  -> {found}" if found else "")
        )
        tail.append(f"{reading['location'] or '—'} / {reading['extent'] or '—'}")
        blocks.append((reading["role"], lines, tail))

    line_h, gap = 21, 12
    height = 16 + sum(26 + line_h * (len(l) + len(t)) + gap for _, l, t in blocks)
    panel = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(panel)
    y = 12
    for role, lines, tail in blocks:
        draw.rectangle((10, y + 4, 16, y + 18), fill=ROLE_COLOR[role])
        draw.text((24, y), role.upper(), font=_font(17), fill=ROLE_COLOR[role])
        y += 24
        for line in lines:
            draw.text((24, y), line, font=_font(18), fill=FG)
            y += line_h
        for line in tail:
            draw.text((24, y), line, font=_font(15), fill=DIM)
            y += line_h
        y += gap
    return panel


def render_reading(images: dict, readings: list[dict], spatial: dict | None, path: Path) -> None:
    base = R.display_base(images, 900)
    panel = _readings_panel(readings, 900, spatial)
    canvas = Image.new("RGB", (900, base.height + panel.height), BG)
    canvas.paste(base, (0, 0))
    canvas.paste(panel, (0, base.height))
    canvas.save(path, format="PNG")


def render_role_masks(images: dict, spatial: dict, path: Path, width: int = 430) -> Image.Image:
    panels = []
    for record in spatial["roles"]:
        base = R.display_base(images, width)
        colour = ROLE_COLOR[record["role"]]
        composed = R._dimmed(base, 0.42)
        for mask in record["masks"]:
            if not isinstance(mask.get("mask"), np.ndarray):
                continue
            layer = R._mask_layer(mask["mask"], base.size)
            tint = Image.new("RGB", base.size, colour)
            composed = Image.composite(Image.blend(base, tint, 0.55), composed, layer)

        draw = ImageDraw.Draw(composed)
        sx, sy = base.width / WORKING, base.height / WORKING
        for mask in record["masks"]:
            if "bbox" not in mask:
                continue
            x0, y0, x1, y1 = mask["bbox"]
            box = (x0 * sx, y0 * sy, x1 * sx, y1 * sy)
            best = mask["mask_index"] == record["best_by_location"]
            draw.rectangle(box, outline=(255, 235, 120) if best else (140, 140, 140),
                           width=3 if best else 1)
            draw.text((box[0] + 4, box[1] + 2), str(mask["mask_index"]),
                      font=_font(18), fill=(255, 235, 120) if best else (200, 200, 200))
        if record["declared_point"]:
            px = record["declared_point"][0] * base.width
            py = record["declared_point"][1] * base.height
            draw.ellipse((px - 9, py - 9, px + 9, py + 9), outline=(120, 255, 200), width=3)

        caption = Image.new("RGB", (width, 78), BG)
        cdraw = ImageDraw.Draw(caption)
        cdraw.text((8, 4), record["role"].upper(), font=_font(19), fill=colour)
        attempts = " ".join(f"{a['phrase']}({a['mask_count']})" for a in record["attempted"]) or "not spatializable"
        cdraw.text((8, 27), attempts[:52], font=_font(14), fill=FG)
        cdraw.text((8, 46), f"declared {record['location'] or '—'} · best#{record['best_by_location']} "
                            f"· largest#{record['largest_mask']}", font=_font(14), fill=DIM)
        stack = Image.new("RGB", (width, composed.height + 78), BG)
        stack.paste(composed, (0, 0))
        stack.paste(caption, (0, composed.height))
        panels.append(stack)

    sheet = _grid(panels, columns=4, title="SEMANTIC ROLE MASKS — yellow box = closest to declared location, green ring = declared point")
    sheet.save(path, format="PNG")
    return sheet


def render_hierarchy(arr_rgb: np.ndarray, hier: dict, out_dir: Path, size) -> dict[int, Image.Image]:
    made = {}
    for level in sorted(hier["levels"], reverse=True):
        image = H.render(arr_rgb, hier["levels"][level], size)
        image.save(out_dir / f"04-hierarchy-{level:02d}.png", format="PNG")
        made[level] = image
    return made


def render_residual(images: dict, residual: dict, hier: dict, arr_rgb: np.ndarray,
                    path: Path, width: int = 560) -> Image.Image:
    base = R.display_base(images, width)
    layer = R._mask_layer(residual["_mask"], base.size)
    tint = Image.new("RGB", base.size, (255, 120, 40))
    overlay = Image.composite(Image.blend(base, tint, 0.5), R._dimmed(base, 0.5), layer)
    panels = [_label(overlay, f"semantic residual — {residual['residual_coverage']:.1%} of frame", width)]

    for level in (12, 6, 3):
        if level not in hier["levels"]:
            continue
        data = hier["levels"][level]
        image = H.render(arr_rgb, data, base.size)
        draw = ImageDraw.Draw(image)
        flagged = [r for r in residual["levels"][str(level)] if r["residual_fraction"] >= 0.7]
        for row in flagged:
            region = next(x for x in data["regions"] if x["index"] == row["region"])
            x0, y0, x1, y1 = region["bbox"]
            sx, sy = base.width / WORKING, base.height / WORKING
            draw.rectangle((x0 * sx, y0 * sy, x1 * sx, y1 * sy), outline=(255, 120, 40), width=3)
            draw.text((x0 * sx + 4, y0 * sy + 2), f"{row['residual_fraction']:.0%}",
                      font=_font(17), fill=(255, 190, 120))
        panels.append(_label(image, f"{level} regions — {len(flagged)} mostly unnamed", width))

    sheet = _grid(panels, columns=4,
                  title="SEMANTIC RESIDUAL vs HIERARCHY — orange = material no reading nominated")
    sheet.save(path, format="PNG")
    return sheet


def _label(image: Image.Image, text: str, width: int) -> Image.Image:
    stack = Image.new("RGB", (width, image.height + 30), BG)
    stack.paste(image, (0, 0))
    ImageDraw.Draw(stack).text((8, image.height + 6), text[:58], font=_font(16), fill=FG)
    return stack


def _grid(panels: list[Image.Image], columns: int, title: str = "") -> Image.Image:
    if not panels:
        return Image.new("RGB", (400, 60), BG)
    cell_w = max(p.width for p in panels)
    cell_h = max(p.height for p in panels)
    rows = (len(panels) + columns - 1) // columns
    header = 40 if title else 6
    sheet = Image.new("RGB", (columns * cell_w + 6 * (columns + 1),
                              header + rows * (cell_h + 6) + 6), BG)
    if title:
        ImageDraw.Draw(sheet).text((10, 10), title, font=_font(19), fill=FG)
    for order, panel in enumerate(panels):
        col, row = order % columns, order // columns
        sheet.paste(panel, (6 + col * (cell_w + 6), header + row * (cell_h + 6)))
    return sheet


def render_sheet(pal: str, entry: dict, images: dict, readings: list[dict], spatial: dict,
                 arr_rgb: np.ndarray, hier: dict, residual: dict,
                 role_sheet: Image.Image, residual_sheet: Image.Image, path: Path) -> None:
    top_w = 1900
    source = R.display_base(images, 950)
    panel = _readings_panel(readings, 950, spatial)
    top = Image.new("RGB", (top_w, max(source.height, panel.height) + 46), BG)
    ImageDraw.Draw(top).text(
        (12, 10),
        f"{pal}  —  {entry['film']} {entry['year'] or ''}  ·  {entry['image_kind']}  ·  E4 compositional reading",
        font=_font(26), fill=FG)
    top.paste(source, (0, 46))
    top.paste(panel, (950, 46))

    strip = []
    for level in (12, 6, 3):
        if level in hier["levels"]:
            strip.append(_label(H.render(arr_rgb, hier["levels"][level], (620, 349)),
                                f"{level} regions (measured mean colours)", 620))
    hier_sheet = _grid(strip, columns=3, title="BOTTOM-UP HIERARCHICAL GROUPING (adjacent merges only)")

    sections = [top, role_sheet, hier_sheet, residual_sheet]
    width = max(s.width for s in sections)
    height = sum(s.height + 10 for s in sections)
    canvas = Image.new("RGB", (width, height), BG)
    y = 0
    for section in sections:
        canvas.paste(section, (0, y))
        y += section.height + 10
    canvas.convert("RGB").save(path, format="JPEG", quality=88)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _json_default(value):
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return "<ndarray>"
    return str(value)


def _strip_masks(spatial: dict) -> dict:
    return {
        "phrases_run": spatial["phrases_run"],
        "roles": [
            {**record, "masks": [{k: v for k, v in m.items() if k != "mask"}
                                 for m in record["masks"]]}
            for record in spatial["roles"]
        ],
    }


def run_one(pal: str, entry: dict, project: Path, corpus_dir: Path, out_root: Path,
            pipeline, index: dict, max_new_tokens: int) -> dict:
    from services.silhouette import load_sam_model

    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = corpus_dir / entry["corpus_image"]
    kind = {"film_frame": "film still", "gameplay_frame": "gameplay frame",
            "poster": "poster"}.get(entry["image_kind"], entry["image_kind"])

    source = {
        "media_id": entry["media_id"], "filename": entry.get("film", ""),
        "media_type": entry["media_type"], "source_path": image_path,
        "title": entry.get("film", ""), "year": str(entry.get("year") or ""),
        "director": entry.get("director", ""),
    }
    annotation = {}
    shot_id = entry.get("shot_id")
    if shot_id and shot_id in index:
        record = index[shot_id]
        annotation = {k: record[k] for k in
                      ("description", "setting", "shot", "time_of_day",
                       "humans", "animals", "objects", "wearing", "action")}

    images = stages.prepare_images(image_path)
    arr_rgb = images["working_rgb"]
    R.save_source(images, out_dir / "01-source.jpg")

    print("  stage 1 — compositional reading …")
    reading_result = read_composition(pipeline, str(project), source, annotation,
                                      kind, images["active"], max_new_tokens)
    readings = reading_result["readings"]
    (out_dir / "02-compositional-reading.json").write_text(
        json.dumps(reading_result, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8")

    print("  stage 2 — SAM3 spatialization of every reading …")
    segmenter, sam_name, device = load_sam_model(str(project), "sam3")
    spatial = spatialize_roles(segmenter, images, readings)
    del segmenter
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass

    render_reading(images, readings, spatial, out_dir / "02-compositional-reading.png")
    role_sheet = render_role_masks(images, spatial, out_dir / "03-role-masks.png")
    (out_dir / "03-role-masks.json").write_text(
        json.dumps(_strip_masks(spatial), indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8")

    print("  stage 3 — SEEDS + adjacency-constrained hierarchy …")
    labels = stages.seeds_labels(arr_rgb)
    hier = H.build(arr_rgb, labels)
    display = R.display_base(images, 900)
    render_hierarchy(arr_rgb, hier, out_dir, display.size)
    (out_dir / "04-hierarchy.json").write_text(
        json.dumps({str(k): {"regions": v["regions"]} for k, v in hier["levels"].items()},
                   indent=2, default=_json_default) + "\n", encoding="utf-8")

    print("  stage 4 — mapping readings onto the hierarchy …")
    role_map = map_roles_to_hierarchy(spatial, hier)
    (out_dir / "05-role-hierarchy-map.json").write_text(
        json.dumps(role_map, indent=2, default=_json_default) + "\n", encoding="utf-8")

    residual = semantic_residual(spatial, hier)
    residual_sheet = render_residual(images, residual, hier, arr_rgb,
                                     out_dir / "06-semantic-residual-vs-hierarchy.png")
    (out_dir / "06-semantic-residual.json").write_text(
        json.dumps({k: v for k, v in residual.items() if k != "_mask"},
                   indent=2, default=_json_default) + "\n", encoding="utf-8")

    inventory = {
        "id": pal,
        "semantic_roles": {
            record["role"]: {
                **{k: v for k, v in reading.items() if k != "role"},
                "mask_count": record["mask_count"],
                "attempted": record["attempted"],
                "best_by_location": record["best_by_location"],
                "largest_mask": record["largest_mask"],
                "deixis_agrees_with_size": record["deixis_agrees_with_size"],
                "hierarchy_overlap": role_map[record["role"]]["levels"],
                "masks": [{k: v for k, v in m.items() if k != "mask"} for m in record["masks"]],
            }
            for reading, record in zip(readings, spatial["roles"])
        },
        "hierarchical_regions": {
            str(level): hier["levels"][level]["regions"] for level in sorted(hier["levels"], reverse=True)
        },
        "semantic_residual": {k: v for k, v in residual.items() if k != "_mask"},
    }
    (out_dir / "07-inventory.json").write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8")

    print("  stage 5 — reading sheet …")
    render_sheet(pal, entry, images, readings, spatial, arr_rgb, hier, residual,
                 role_sheet, residual_sheet, out_dir / "08-reading-sheet.jpg")

    manifest = {
        "id": pal, "status": "ok",
        "experiment": EXPERIMENT, "space": SPACE,
        "created": datetime.now(timezone.utc).isoformat(),
        "corpus_source": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
        "media_id": entry["media_id"], "shot_id": entry.get("shot_id"),
        "film": entry["film"], "year": entry["year"], "image_kind": entry["image_kind"],
        "annotation_present": bool(annotation),
        "models": {"interpreter": "Qwen3-VL-8B-Instruct", "segmenter": sam_name, "device": device},
        "prompts": {"system": reading_result["system_file"], "user": reading_result["user_file"]},
        "settings": {
            "working_size": WORKING, "sam_resolution": "working",
            "seeds_superpixels": int(hier["superpixel_count"]),
            "hierarchy_levels": sorted(hier["levels"], reverse=True),
            "merge_rule": "adjacent-only, Ward increment in CIELAB",
            "max_new_tokens": max_new_tokens,
        },
        "roles_present": [r["role"] for r in readings if not r["is_none"]],
        "roles_none": [r["role"] for r in readings if r["is_none"]],
        "roles_spatializable": [r["role"] for r in readings if r["declared_spatializable"]],
        "roles_with_masks": [rec["role"] for rec in spatial["roles"] if rec["mask_count"]],
        "residual_coverage": residual["residual_coverage"],
    }
    (out_dir / "00-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8")
    return manifest


def main(max_new_tokens: int = 2048) -> Path:
    from data.annotate import _load_text_generation_pipeline
    from tool import prefs

    project = Path(prefs.get("path"))
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    corpus = {e["id"]: e for e in
              json.loads((corpus_dir / "corpus-manifest.json").read_text())["images"]}
    index = {r["shot_id"]: r for r in C.load(str(project)) if r.get("shot_id")}

    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    out_root.mkdir(parents=True, exist_ok=True)

    print("loading interpreter …")
    pipeline = _load_text_generation_pipeline(str(project), "Qwen3-VL-8B-Instruct")

    results = []
    for pal in PILOT:
        entry = corpus[pal]
        print(f"\n=== {pal}  {entry['film'][:32]}  ({entry['image_kind']})")
        try:
            results.append(run_one(pal, entry, project, corpus_dir, out_root,
                                   pipeline, index, max_new_tokens))
        except Exception as exc:
            print(f"  !! {type(exc).__name__}: {exc}")
            results.append({"id": pal, "status": "runner_error",
                            "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc()[-1200:]})

    (out_root / "pilot-manifest.json").write_text(
        json.dumps({"space": SPACE, "experiment": EXPERIMENT,
                    "created": datetime.now(timezone.utc).isoformat(),
                    "images": PILOT, "results": results},
                   indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8")
    print(f"\npilot complete -> {out_root}")
    return out_root


if __name__ == "__main__":
    main()
