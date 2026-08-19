"""E3 — run the frozen 30-image research corpus through Palette System 2.0.

A thin driver only.  It resolves each PAL entry from the frozen corpus manifest
and calls ``run_experiment.run()`` unchanged; models are loaded once and reused
across images so the apparatus is identical but not reloaded thirty times.

    uv run python -m scripts.palette_lab.run_corpus
"""

from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

from scripts.palette_lab import contact_sheet as CS
from scripts.palette_lab import corpus as C
from scripts.palette_lab import run_experiment as RX

SPACE = "palette-system-2026-08-18"
EXPERIMENT = "E3-corpus"

KIND_LABEL = {
    "film_frame": "film still",
    "gameplay_frame": "gameplay frame",
    "poster": "poster",
}


def _annotation_for(entry: dict, index: dict) -> dict:
    """Full shot annotation for a corpus entry; empty dict for posters."""
    shot_id = entry.get("shot_id")
    if not shot_id or shot_id not in index:
        return {}
    record = index[shot_id]
    return {
        "description": record["description"],
        "setting": record["setting"],
        "shot": record["shot"],
        "time_of_day": record["time_of_day"],
        "humans": record["humans"],
        "animals": record["animals"],
        "objects": record["objects"],
        "wearing": record["wearing"],
        "action": record["action"],
    }


def run_all(project: Path) -> Path:
    corpus_dir = project / "outputs" / "tests" / SPACE / "corpus"
    manifest = json.loads((corpus_dir / "corpus-manifest.json").read_text(encoding="utf-8"))
    entries = manifest["images"]

    index = {r["shot_id"]: r for r in C.load(str(project)) if r.get("shot_id")}

    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    out_root.mkdir(parents=True, exist_ok=True)

    from data.annotate import _load_text_generation_pipeline

    base_args = RX.build_parser().parse_args([
        "--media", "unused", "--space", SPACE, "--experiment", EXPERIMENT,
        "--curator", "both",
    ])

    # Only the interpreter is reused across images. SAM3 is still loaded and
    # freed inside each run, exactly as in the E1 baseline, so the per-image
    # GPU footprint is unchanged.
    print("loading interpreter once for the whole corpus …")
    pipeline = _load_text_generation_pipeline(str(project), base_args.model)
    print(f"  Qwen={base_args.model}  SAM3={base_args.sam_model}\n")

    results = []
    for entry in entries:
        pal = entry["id"]
        image_path = corpus_dir / entry["corpus_image"]
        kind = KIND_LABEL.get(entry["image_kind"], entry["image_kind"])
        print(f"=== {pal}  {entry['film'][:34]}  ({entry['image_kind']})")

        args = RX.build_parser().parse_args([
            "--media", "unused", "--space", SPACE, "--experiment", EXPERIMENT,
            "--curator", "both", "--kind", kind,
        ])
        source_override = {
            "media_id": entry["media_id"],
            "filename": entry.get("film", ""),
            "media_type": entry["media_type"],
            "source_path": image_path,
            "title": entry.get("film", ""),
            "year": str(entry.get("year") or ""),
            "director": entry.get("director", ""),
        }
        annotation = _annotation_for(entry, index)

        record = {"id": pal, "status": "ok", "notes": []}
        try:
            out_dir = RX.run(
                args,
                pipeline=pipeline,
                source_override=source_override,
                annotation_override=annotation,
                out_name=pal,
            )
            record["directory"] = str(out_dir.relative_to(out_root))
        except Exception as exc:
            record["status"] = "runner_error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["traceback"] = traceback.format_exc()[-1500:]
            print(f"  !! runner error: {exc}")
            (out_root / pal).mkdir(parents=True, exist_ok=True)
            RX._write_json(out_root / pal / "00-manifest.json", record)
        results.append(record)

    (out_root / "_run-log.json").write_text(
        json.dumps({"started": datetime.now(timezone.utc).isoformat(), "results": results},
                   indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"\nrun complete -> {out_root}")
    return out_root


# ---------------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------------

def _candidate(measurements: dict, region: str, index) -> dict | None:
    for candidate in (measurements.get(region) or {}).get("candidates", []):
        if candidate["index"] == index:
            return candidate
    return None


def collect(project: Path) -> dict:
    corpus_dir = project / "outputs" / "tests" / SPACE / "corpus"
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    corpus = {e["id"]: e for e in
              json.loads((corpus_dir / "corpus-manifest.json").read_text())["images"]}

    records = []
    for pal in sorted(corpus):
        directory = out_root / pal
        entry = corpus[pal]
        record = {
            "id": pal,
            "status": "missing",
            "film": entry["film"],
            "year": entry["year"],
            "image_kind": entry["image_kind"],
            "research_tags": entry["research_tags"],
            "comparison_group": entry["comparison_group"],
            "research_question": entry["research_question"],
            "anticipated_failure": entry["anticipated_failure"],
            "notes": [],
        }

        manifest_path = directory / "00-manifest.json"
        if not manifest_path.exists():
            records.append(record)
            continue
        manifest = json.loads(manifest_path.read_text())
        record["status"] = manifest.get("status", "unknown")

        focus_path = directory / "02-focus.json"
        if focus_path.exists():
            focus = json.loads(focus_path.read_text())
            parsed = focus.get("parsed") or {}
            block = parsed.get("focus") or {}
            ambience = parsed.get("ambience") or {}
            record.update({
                "focus_description": block.get("description", ""),
                "focus_segment_phrase": block.get("segment_phrase", ""),
                "focus_type": block.get("type", ""),
                "focus_reason": block.get("reason", ""),
                "focus_confidence": block.get("confidence", ""),
                "ambience_description": ambience.get("description", ""),
                "ambience_segment_phrase": ambience.get("segment_phrase", ""),
                "expected_appearance": parsed.get("expected_appearance", ""),
                "alternative_focus": (parsed.get("alternative_focus") or {}).get("description", ""),
            })
            if not parsed:
                record["notes"].append("interpreter returned no parsable JSON")

        spatial_path = directory / "03-spatialization.json"
        if spatial_path.exists():
            spatial = json.loads(spatial_path.read_text())
            for role in ("focus", "ambience"):
                block = spatial.get(role) or {}
                record[f"{role}_accepted_phrase"] = block.get("accepted_phrase")
                record[f"{role}_ladder_rung"] = block.get("ladder_rung")
                record[f"{role}_mask_count"] = len(spatial.get(f"{role}_masks") or [])
                record[f"{role}_attempts"] = [
                    {"phrase": a["phrase"], "masks": a["mask_count"]}
                    for a in block.get("attempts") or []
                ]
                if block.get("accepted_phrase") is None:
                    record["notes"].append(f"{role}: SAM3 returned no masks")

        if record["status"] == "focus_not_spatialized":
            records.append(record)
            continue

        decomposition = manifest.get("decomposition") or {}
        record.update({
            "focus_coverage": decomposition.get("focus_pixel_coverage"),
            "ambience_coverage": decomposition.get("ambience_pixel_coverage"),
            "residual_coverage": decomposition.get("residual_pixel_coverage"),
            "focus_superpixels": decomposition.get("focus_superpixels"),
            "boundary_superpixels": decomposition.get("boundary_superpixels"),
        })

        candidates_path = directory / "09-candidates.json"
        measurements = json.loads(candidates_path.read_text()) if candidates_path.exists() else {}
        for region in ("focus", "ambience"):
            block = measurements.get(region) or {}
            record[f"{region}_candidate_count"] = len(block.get("candidates") or [])
            if block.get("error"):
                record["notes"].append(f"{region} candidates: {block['error']}")

        curator_path = directory / "12-curator.json"
        if curator_path.exists():
            curator = json.loads(curator_path.read_text())
            for arm in ("json", "visual"):
                result = (curator.get("arms") or {}).get(arm) or {}
                parsed = result.get("parsed") or {}
                if not parsed:
                    record["notes"].append(f"{arm} curator returned no parsable JSON")
                for region in ("focus", "ambience"):
                    index = parsed.get(f"{region}_index")
                    candidate = _candidate(measurements, region, index)
                    record[f"{arm}_{region}_index"] = index
                    record[f"{arm}_{region}_rgb"] = candidate["rgb"] if candidate else None
                    record[f"{arm}_{region}_hex"] = candidate["hex"] if candidate else None
                    record[f"{arm}_{region}_is"] = parsed.get(f"{region}_is", "")
                    record[f"{arm}_{region}_reason"] = parsed.get(f"{region}_reason", "")
                    if index is not None and candidate is None:
                        record["notes"].append(f"{arm} {region}: index {index} not in candidate list")
                record[f"{arm}_confidence"] = parsed.get("confidence", "")
                record[f"{arm}_notes"] = parsed.get("notes", "")
                record[f"{arm}_rejected"] = parsed.get("focus_rejected", "")

        for region in ("focus", "ambience"):
            json_index = record.get(f"json_{region}_index")
            visual_index = record.get(f"visual_{region}_index")
            record[f"{region}_agreement"] = (
                json_index is not None and json_index == visual_index
            )

        records.append(record)

    summary = {
        "space": SPACE,
        "experiment": EXPERIMENT,
        "collected": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "descriptive_counts": {
            "runs_completed": sum(1 for r in records if r["status"] == "ok"),
            "runner_errors": sum(1 for r in records if r["status"] == "runner_error"),
            "focus_not_spatialized": sum(1 for r in records if r["status"] == "focus_not_spatialized"),
            "focus_disagreements": sum(1 for r in records if r.get("focus_agreement") is False and r["status"] == "ok"),
            "ambience_disagreements": sum(1 for r in records if r.get("ambience_agreement") is False and r["status"] == "ok"),
            "zero_mask_focus": sum(1 for r in records if r.get("focus_mask_count") == 0),
            "zero_mask_ambience": sum(1 for r in records if r.get("ambience_mask_count") == 0),
            "invalid_curator_responses": sum(
                1 for r in records for n in r["notes"] if "curator returned no parsable JSON" in n
            ),
        },
        "note": "Descriptive counts only. No ground truth exists; these are not accuracy scores.",
        "results": records,
    }
    (out_root / "results-manifest.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    return summary


# ---------------------------------------------------------------------------
# Result walls
# ---------------------------------------------------------------------------

BG = (16, 16, 18)
FG = (238, 238, 238)
DIM = (150, 150, 158)


def _swatch_row(width: int, height: int, pairs: list[tuple[str, list | None]]):
    strip = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(strip)
    cell = width // len(pairs)
    font = CS._font(15)
    for order, (label, rgb) in enumerate(pairs):
        x = order * cell
        if rgb:
            draw.rectangle((x + 2, 2, x + cell - 3, height - 18), fill=tuple(rgb))
        else:
            draw.rectangle((x + 2, 2, x + cell - 3, height - 18), outline=(90, 60, 60), width=2)
            draw.text((x + 10, height // 2 - 14), "none", font=font, fill=(180, 90, 90))
        draw.text((x + 3, height - 17), label, font=font, fill=DIM)
    return strip


def result_wall(project: Path, summary: dict) -> list[Path]:
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    corpus_dir = project / "outputs" / "tests" / SPACE / "corpus"
    corpus = {e["id"]: e for e in
              json.loads((corpus_dir / "corpus-manifest.json").read_text())["images"]}

    cell_w, img_h, swatch_h, cap_h, gutter = 400, 225, 54, 62, 8
    columns, rows = 5, 6
    block_h = img_h + swatch_h + cap_h
    canvas = Image.new(
        "RGB",
        (columns * cell_w + gutter * (columns + 1), 78 + rows * (block_h + gutter) + gutter),
        BG,
    )
    draw = ImageDraw.Draw(canvas)
    draw.text((14, 24),
              "Palette System 2.0 — E3 corpus result wall     swatches: VF = visual focus, VA = visual ambience, JF = json focus, JA = json ambience",
              font=CS._font(24), fill=FG)

    id_font, line_font, tag_font = CS._font(21), CS._font(16), CS._font(14)
    for order, record in enumerate(summary["results"]):
        pal = record["id"]
        col, row = order % columns, order // columns
        x = gutter + col * (cell_w + gutter)
        y = 78 + row * (block_h + gutter)

        entry = corpus[pal]
        canvas.paste(CS._fit(str(corpus_dir / entry["corpus_image"]), cell_w, img_h), (x, y))
        canvas.paste(
            _swatch_row(cell_w, swatch_h, [
                ("VF", record.get("visual_focus_rgb")),
                ("VA", record.get("visual_ambience_rgb")),
                ("JF", record.get("json_focus_rgb")),
                ("JA", record.get("json_ambience_rgb")),
            ]),
            (x, y + img_h),
        )
        cy = y + img_h + swatch_h + 3
        draw.text((x + 3, cy), pal, font=id_font, fill=(232, 176, 92))
        flag = ""
        if record["status"] != "ok":
            flag = f"  [{record['status']}]"
        elif not record.get("focus_agreement") or not record.get("ambience_agreement"):
            marks = []
            if not record.get("focus_agreement"):
                marks.append("focus")
            if not record.get("ambience_agreement"):
                marks.append("ambience")
            flag = "  ≠ " + "/".join(marks)
        draw.text((x + 92, cy + 2), (entry["film"][:26] + flag)[:40], font=line_font, fill=FG)
        draw.text((x + 3, cy + 24), f"{entry['year'] or 'game'}  {entry['image_kind'].replace('_', ' ')}",
                  font=tag_font, fill=DIM)
        draw.text((x + 3, cy + 42), "  ".join(entry["research_tags"][:3])[:56],
                  font=tag_font, fill=(120, 170, 210))

    path = out_root / "result-wall.jpg"
    canvas.save(path, format="JPEG", quality=90)

    written = [path]
    for region in ("focus", "ambience"):
        cw, ih, sh, ch = 330, 186, 46, 46
        cols, rws = 6, 5
        bh = ih + sh + ch
        sheet = Image.new("RGB", (cols * cw + gutter * (cols + 1), 66 + rws * (bh + gutter) + gutter), BG)
        sdraw = ImageDraw.Draw(sheet)
        sdraw.text((14, 20), f"E3 corpus — {region} selections     left = JSON arm, right = visual arm",
                   font=CS._font(22), fill=FG)
        for order, record in enumerate(summary["results"]):
            col, row = order % cols, order // cols
            x = gutter + col * (cw + gutter)
            y = 66 + row * (bh + gutter)
            entry = corpus[record["id"]]
            sheet.paste(CS._fit(str(corpus_dir / entry["corpus_image"]), cw, ih), (x, y))
            sheet.paste(
                _swatch_row(cw, sh, [
                    ("JSON", record.get(f"json_{region}_rgb")),
                    ("VISUAL", record.get(f"visual_{region}_rgb")),
                ]),
                (x, y + ih),
            )
            cy = y + ih + sh + 2
            sdraw.text((x + 3, cy), record["id"], font=CS._font(18), fill=(232, 176, 92))
            agree = record.get(f"{region}_agreement")
            sdraw.text((x + 84, cy + 1), ("same" if agree else "DIFFERENT"), font=tag_font,
                       fill=(120, 190, 130) if agree else (235, 140, 110))
            sdraw.text((x + 3, cy + 22), entry["film"][:38], font=tag_font, fill=DIM)
        p = out_root / f"result-wall-{region}.jpg"
        sheet.save(p, format="JPEG", quality=90)
        written.append(p)
    return written


def disagreements(project: Path, summary: dict) -> Path:
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    lines = [
        "# E3 corpus — curator disagreement index",
        "",
        "Images where the JSON arm and the visual arm selected different measured candidates.",
        "No judgement is made about which is correct.",
        "",
    ]
    for region in ("focus", "ambience"):
        rows = [r for r in summary["results"]
                if r["status"] == "ok" and r.get(f"{region}_agreement") is False]
        lines += [f"## {region.upper()} — {len(rows)} disagreements", ""]
        for record in rows:
            lines += [
                f"### {record['id']} — {record['film']} ({record['year'] or 'gameplay'})",
                "",
                f"- **Interpreted focus** — {record.get('focus_description', '')}",
                f"- **Segmented as** — `{record.get('focus_accepted_phrase')}`",
                f"- **JSON arm** — candidate {record.get(f'json_{region}_index')} "
                f"`{record.get(f'json_{region}_hex')}` — {record.get(f'json_{region}_is', '')}",
                f"- **Visual arm** — candidate {record.get(f'visual_{region}_index')} "
                f"`{record.get(f'visual_{region}_hex')}` — {record.get(f'visual_{region}_is', '')}",
                "",
                f"> JSON: {str(record.get(f'json_{region}_reason', ''))[:300]}",
                "",
                f"> VISUAL: {str(record.get(f'visual_{region}_reason', ''))[:300]}",
                "",
                f"Candidates: `{record['id']}/"
                f"{'09-focus-candidates.png' if region == 'focus' else '10-ambience-candidates.png'}` · "
                f"sheet: `{record['id']}/11-candidate-sheet.png`",
                "",
            ]
    path = out_root / "disagreements.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    from tool import prefs

    project = Path(prefs.get("path"))
    run_all(project)
    summary = collect(project)
    result_wall(project, summary)
    disagreements(project, summary)
    counts = summary["descriptive_counts"]
    print("\ndescriptive counts:")
    for key, value in counts.items():
        print(f"  {key:26s} {value}")


if __name__ == "__main__":
    main()
