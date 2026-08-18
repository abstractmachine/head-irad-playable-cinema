"""Palette System 2.0 — experiment runner.

Runs one image through the hybrid loop and writes a browsable directory of
visual artifacts.  Production palette behaviour is not touched: this script
imports production components and writes only under
``<project>/outputs/tests/``.

    uv run python -m scripts.palette_lab.run_experiment --media game_rdr2_ce5e0bba
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from scripts.palette_lab import prompts as prompt_io
from scripts.palette_lab import render, stages

DEFAULT_SPACE = "palette-system-2026-08-18"


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _git_revision(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _free_cuda() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _candidate_rgb(measurement: dict, index) -> list | None:
    return next(
        (c["rgb"] for c in measurement.get("candidates", []) if c["index"] == index),
        None,
    )


def _production_palette(project_path: str, media_id: str, media_type: str) -> dict | None:
    from data.palette import get_thumbnail_palette_path

    path = get_thumbnail_palette_path(project_path, media_id, media_type)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def run(args: argparse.Namespace) -> Path:
    from tool import prefs

    project_path = args.project or prefs.get("path")
    if not project_path:
        raise SystemExit("No project path configured. Pass --project.")
    repo_root = Path(__file__).resolve().parents[2]

    source = stages.resolve_source(project_path, args.media, args.media_type)
    annotation = None if args.mode == "context" else stages.resolve_annotation(project_path, source)
    images = stages.prepare_images(source["source_path"])

    out_dir = (
        Path(project_path) / "outputs" / "tests" / args.space / args.experiment / source["media_id"]
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates_dir = out_dir / "candidates"
    candidates_dir.mkdir(exist_ok=True)

    started = datetime.now(timezone.utc).isoformat()
    print(f"  project     : {project_path}")
    print(f"  source      : {source['source_path']}")
    print(f"  output      : {out_dir}")
    print(f"  mode        : {args.mode} (annotation {'present' if annotation else 'absent'})")

    render.save_source(images, out_dir / "01-source.jpg")

    # --- Stage 1: Qwen interpreter -----------------------------------------
    focus_prompts = prompt_io.load_pair(
        project_path, "focus",
        system_file=args.focus_system, user_file=args.focus_user,
    )
    focus_vars = stages.focus_variables(source, annotation, args.kind)
    focus_system = prompt_io.strip_comments(focus_prompts["system"])
    focus_user = prompt_io.substitute(
        prompt_io.strip_comments(focus_prompts["user"]), focus_vars
    )

    print(f"  loading {args.model} …")
    from data.annotate import _load_text_generation_pipeline

    pipeline = _load_text_generation_pipeline(project_path, args.model)

    print("  stage 1 — Qwen interpretation …")
    focus = stages.call_qwen(
        pipeline, focus_system, focus_user, [images["active"]], args.max_new_tokens
    )
    focus["system_file"] = focus_prompts["system_file"]
    focus["user_file"] = focus_prompts["user_file"]
    focus["variables"] = focus_vars
    _write_json(out_dir / "02-focus.json", focus)
    render.render_interpretation(images, focus, out_dir / "02-focus.png")

    parsed_focus = focus.get("parsed") or {}
    if not parsed_focus:
        print("  ! interpreter returned no parsable JSON — see 02-focus.json")

    # --- Stage 2: SAM3 spatialization --------------------------------------
    print(f"  stage 2 — SAM3 spatialization ({args.sam_resolution}) …")
    from services.silhouette import load_sam_model

    segmenter, sam_name, device = load_sam_model(project_path, args.sam_model)

    focus_ladder = stages._phrase_ladder(parsed_focus.get("focus"))
    ambience_ladder = stages._phrase_ladder(parsed_focus.get("ambience"))
    focus_spatial = stages.spatialize(
        segmenter, images, focus_ladder, resolution=args.sam_resolution
    )
    ambience_spatial = stages.spatialize(
        segmenter, images, ambience_ladder, resolution=args.sam_resolution
    )

    del segmenter
    _free_cuda()

    _write_json(out_dir / "03-spatialization.json", {
        "focus": {k: v for k, v in focus_spatial.items() if k not in ("union", "masks")},
        "ambience": {k: v for k, v in ambience_spatial.items() if k not in ("union", "masks")},
        "focus_masks": focus_spatial["masks"] and [
            {k: v for k, v in m.items() if k != "mask"} for m in focus_spatial["masks"]
        ],
        "ambience_masks": ambience_spatial["masks"] and [
            {k: v for k, v in m.items() if k != "mask"} for m in ambience_spatial["masks"]
        ],
        "sam_model": sam_name,
        "device": device,
    })
    render.render_masks(images, focus_spatial, out_dir / "04-focus-mask.png", role="focus")
    render.render_masks(images, ambience_spatial, out_dir / "05-ambience-mask.png", role="ambience")

    if not focus_spatial["union"].any():
        print("  ! SAM3 found nothing for the focus — the run stops here by design")
        _write_json(out_dir / "00-manifest.json", {
            "status": "focus_not_spatialized",
            "started": started,
            "source": source,
            "focus_ladder": focus_ladder,
        })
        return out_dir

    # --- Stage 3: SEEDS -----------------------------------------------------
    print("  stage 3 — SEEDS decomposition …")
    decomposition = stages.decompose(
        images["working_rgb"], focus_spatial["union"], ambience_spatial["union"]
    )
    labels = decomposition["labels"]
    render.render_seeds(images, labels, out_dir / "06-seeds.png")
    render.render_ownership(images, decomposition, out_dir / "07-superpixel-ownership.png")
    _write_json(out_dir / "06-superpixels.json", {
        "summary": decomposition["summary"],
        "superpixels": decomposition["superpixels"],
    })

    # --- Stage 4: LAB / Ward candidates ------------------------------------
    print("  stage 4 — LAB / Ward candidate measurement …")
    measurements = {
        region: stages.measure_candidates(
            images["working_rgb"], decomposition, region, n_candidates=args.candidates
        )
        for region in ("focus", "ambience")
    }
    _write_json(out_dir / "09-candidates.json", measurements)
    render.render_ward_clusters(images, decomposition, measurements, out_dir / "08-ward-clusters.png")

    for region, filename in (("focus", "09-focus-candidates.png"), ("ambience", "10-ambience-candidates.png")):
        render.render_candidate_sheet(
            images, labels, decomposition, measurements[region],
            out_dir / filename, region=region,
        )
        for candidate in measurements[region].get("candidates", []):
            render.render_candidate(
                images, labels, decomposition["masks"][region], candidate,
                candidates_dir / f"{region}-{candidate['index']:02d}.png",
                region=region,
            )

    sheet_path = out_dir / "11-candidate-sheet.png"
    render.render_curator_sheet(images, labels, decomposition, measurements, sheet_path)

    # --- Stage 5: Qwen curator ---------------------------------------------
    curator_prompts = prompt_io.load_pair(
        project_path, "curator",
        system_file=args.curator_system, user_file=args.curator_user,
    )
    curator_system = prompt_io.strip_comments(curator_prompts["system"])
    focus_block = parsed_focus.get("focus") or {}
    ambience_block = parsed_focus.get("ambience") or {}

    arms = ["json", "visual"] if args.curator == "both" else [args.curator]
    curator_results: dict = {}
    for arm in arms:
        attached = (
            "The source image is attached."
            if arm == "json"
            else "The source image is attached, followed by a candidate visualization sheet."
        )
        curator_user = prompt_io.substitute(
            prompt_io.strip_comments(curator_prompts["user"]),
            {
                "kind": args.kind,
                "title": source.get("title") or source["filename"],
                "year": source.get("year", ""),
                "focusdesc": str(focus_block.get("description", "")),
                "ambiencedesc": str(ambience_block.get("description", "")),
                "focusphrase": str(focus_spatial.get("accepted_phrase") or ""),
                "ambiencephrase": str(ambience_spatial.get("accepted_phrase") or ""),
                "focuscandidates": stages.candidate_table(measurements["focus"]),
                "ambiencecandidates": stages.candidate_table(measurements["ambience"]),
                "attached": attached,
            },
        )
        payload = [images["active"]]
        if arm == "visual":
            with Image.open(sheet_path) as sheet:
                payload.append(sheet.convert("RGB"))

        print(f"  stage 5 — Qwen curator ({arm}) …")
        result = stages.call_qwen(
            pipeline, curator_system, curator_user, payload, args.max_new_tokens
        )
        result["arm"] = arm
        result["user_prompt"] = curator_user
        curator_results[arm] = result
        render.render_selection(
            images, labels, decomposition, measurements, result,
            out_dir / f"12-curatorial-selection-{arm}.png",
        )

    _write_json(out_dir / "12-curator.json", {
        "system_file": curator_prompts["system_file"],
        "user_file": curator_prompts["user_file"],
        "arms": curator_results,
    })

    # --- Manifest -----------------------------------------------------------
    production = _production_palette(project_path, source["media_id"], source["media_type"])
    if production:
        last = curator_results.get(arms[-1], {}).get("parsed") or {}
        render.render_production_comparison(
            images,
            {
                "production foreground": production.get("foreground", {}).get("rgb"),
                "production background": production.get("background", {}).get("rgb"),
                "lab focus": _candidate_rgb(measurements["focus"], last.get("focus_index")),
                "lab ambience": _candidate_rgb(measurements["ambience"], last.get("ambience_index")),
            },
            out_dir / "13-production-comparison.png",
        )

    _write_json(out_dir / "00-manifest.json", {
        "status": "ok",
        "experiment": args.experiment,
        "space": args.space,
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "code_revision": _git_revision(repo_root),
        "source": {**source, "source_path": str(source["source_path"])},
        "mode": args.mode,
        "annotation_present": annotation is not None,
        "annotation": annotation,
        "image_kind": args.kind,
        "models": {"interpreter": args.model, "segmenter": sam_name, "device": device},
        "prompts": {
            "focus_system": focus_prompts["system_file"],
            "focus_user": focus_prompts["user_file"],
            "curator_system": curator_prompts["system_file"],
            "curator_user": curator_prompts["user_file"],
        },
        "settings": {
            "sam_resolution": args.sam_resolution,
            "working_size": list(images["working"].size),
            "seeds_target": stages._SEEDS_TARGET_SUPERPIXELS,
            "seeds_iterations": stages._SEEDS_ITERATIONS,
            "ward_clusters": stages._FIG_N_CLUSTERS,
            "candidates_kept": args.candidates,
            "max_new_tokens": args.max_new_tokens,
            "curator_arms": arms,
            "letterbox": images["content"],
        },
        "focus_ladder": focus_ladder,
        "ambience_ladder": ambience_ladder,
        "spatialization": {
            "focus_accepted": focus_spatial.get("accepted_phrase"),
            "focus_rung": focus_spatial.get("ladder_rung"),
            "ambience_accepted": ambience_spatial.get("accepted_phrase"),
            "ambience_rung": ambience_spatial.get("ladder_rung"),
        },
        "decomposition": decomposition["summary"],
        "production_palette": {
            "foreground": (production or {}).get("foreground", {}).get("rgb"),
            "background": (production or {}).get("background", {}).get("rgb"),
        } if production else None,
        "curator_selection": {
            arm: {
                "focus_index": (result.get("parsed") or {}).get("focus_index"),
                "ambience_index": (result.get("parsed") or {}).get("ambience_index"),
            }
            for arm, result in curator_results.items()
        },
    })

    print(f"\n  done → {out_dir}")
    return out_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Palette System 2.0 research run")
    parser.add_argument("--media", required=True, help="media_id or filename fragment")
    parser.add_argument("--media-type", default="gameplay", choices=["movie", "gameplay"])
    parser.add_argument("--project", default=None, help="project path (default: active project)")
    parser.add_argument("--space", default=DEFAULT_SPACE)
    parser.add_argument("--experiment", default="E1-baseline")
    parser.add_argument("--mode", default="annotated", choices=["annotated", "context"])
    parser.add_argument("--kind", default="gameplay frame")
    parser.add_argument("--curator", default="both", choices=["json", "visual", "both"])
    parser.add_argument("--sam-resolution", default="working", choices=["working", "active"])
    parser.add_argument("--candidates", type=int, default=8)
    parser.add_argument("--model", default="Qwen3-VL-8B-Instruct")
    parser.add_argument("--sam-model", default="sam3")
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--focus-system", default=None)
    parser.add_argument("--focus-user", default=None)
    parser.add_argument("--curator-system", default=None)
    parser.add_argument("--curator-user", default=None)
    return parser


if __name__ == "__main__":
    sys.exit(0 if run(build_parser().parse_args()) else 1)
