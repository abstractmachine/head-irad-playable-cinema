"""E5 — reduce the frozen E4 pilot to exactly ``focus`` and ``ambiance``.

E5 does not run Qwen, modify E4, or touch the production palette.  It replays
only the SAM3 phrases already recorded by E4 to recover semantic mask pixels,
then measures a common semantic/hierarchy candidate pool and evaluates ordered
pairs.  Every output is written below ``outputs/tests``.

    uv run python -m scripts.palette_lab.e5_two_color --project /path/to/project
"""

from __future__ import annotations

import argparse
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from scripts.palette_lab import e5_render
from scripts.palette_lab import render as R
from scripts.palette_lab import stages
from scripts.palette_lab import two_color as E5
from scripts.palette_lab.e4_pilot import CORPUS_SPACE, PILOT, SPACE


EXPERIMENT = "E5-two-color"
E4_EXPERIMENT = "E4-pilot"


def _json_default(value):
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return "<ndarray>"
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _free_cuda() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _source(entry: dict, image_path: Path) -> dict:
    return {
        "media_id": entry["media_id"],
        "filename": entry.get("film", ""),
        "media_type": entry["media_type"],
        "source_path": image_path,
        "title": entry.get("film", ""),
        "year": str(entry.get("year") or ""),
        "director": entry.get("director", ""),
    }


def _disagreement(candidates: list[dict], unavailable: list[dict], reduction: dict) -> dict:
    selected = {
        role: next(
            candidate for candidate in candidates
            if candidate["candidate_id"] == reduction["roles"][role]["candidate_id"]
        )
        for role in ("focus", "ambiance")
    }
    replay_drift = []
    for candidate in candidates:
        if candidate["source"] != "semantic":
            continue
        replay = candidate["semantic"]["replay"]
        if abs(replay["area_delta_fraction"]) >= 0.05 or (replay["centroid_drift"] or 0) >= 0.05:
            replay_drift.append({
                "candidate_id": candidate["candidate_id"],
                **replay,
            })
    return {
        "unavailable_e4_evidence": unavailable,
        "semantic_candidates_without_hierarchy_overlap": [
            candidate["candidate_id"] for candidate in candidates
            if candidate["source"] == "semantic"
            and not candidate.get("cross_source", {}).get("hierarchy_overlaps")
        ],
        "hierarchy_candidates_without_semantic_overlap": [
            candidate["candidate_id"] for candidate in candidates
            if candidate["source"] == "hierarchy"
            and not candidate.get("cross_source", {}).get("semantic_overlaps")
        ],
        "semantic_replay_drift": replay_drift,
        "selected_pair_cross_source_evidence": {
            role: {
                "source": candidate["source"],
                "best_overlap_iou": candidate.get("cross_source", {}).get("best_overlap_iou"),
                "mutual_majority": candidate.get("cross_source", {}).get("mutual_majority"),
            }
            for role, candidate in selected.items()
        },
        "same_exact_rgb": reduction["pair_metrics"]["same_exact_rgb"],
    }


def run_one(pal: str, entry: dict, project: Path, corpus_dir: Path, e4_root: Path,
            out_root: Path, segmenter, sam_name: str, device: str) -> dict:
    """Run the E5 reducer for one frozen E4 record."""
    e4_dir = e4_root / pal
    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    e4_manifest = _read_json(e4_dir / "00-manifest.json")
    if e4_manifest.get("status") != "ok":
        raise RuntimeError(f"E4 record is not usable: {e4_manifest.get('status')}")
    hierarchy = _read_json(e4_dir / "04-hierarchy.json")
    inventory = _read_json(e4_dir / "07-inventory.json")
    residual = _read_json(e4_dir / "06-semantic-residual.json")
    image_path = corpus_dir / entry["corpus_image"]
    images = stages.prepare_images(image_path)
    R.save_source(images, out_dir / "01-source.jpg")

    recovered_masks, recovery_report = E5.recover_hierarchy_masks(hierarchy, e4_dir)
    replayed, replay_report = E5.replay_e4_phrases(segmenter, images["working"], inventory)
    semantic, unavailable = E5.semantic_candidates(
        inventory, replayed, images["working_rgb"]
    )
    hierarchy_pool = E5.hierarchy_candidates(hierarchy, recovered_masks, residual)
    candidates = semantic + hierarchy_pool
    E5.add_cross_source_evidence(candidates)
    E5.add_hierarchy_persistence(candidates)
    reduction = E5.reduce_to_two_colours(candidates)
    disagreement = _disagreement(candidates, unavailable, reduction)

    _write_json(out_dir / "02-replay-provenance.json", {
        "purpose": "Recover E5 semantic pixels without rerunning Qwen or E4 hierarchy.",
        "frozen_e4": {
            "manifest": "../E4-pilot/%s/00-manifest.json" % pal,
            "role_masks": "../E4-pilot/%s/03-role-masks.json" % pal,
            "hierarchy": "../E4-pilot/%s/04-hierarchy.json" % pal,
        },
        "semantic_phrase_replay": replay_report,
        "hierarchy_snapshot_recovery": recovery_report,
    })
    _write_json(out_dir / "03-candidate-inventory.json", {
        "measured_candidates": E5.public_candidates(candidates),
        "retained_unavailable_evidence": unavailable,
    })
    _write_json(out_dir / "04-pair-ranking.json", {
        "candidate_ranking": [
            {
                "candidate_id": candidate["candidate_id"],
                "source": candidate["source"],
                "focus": candidate["ranking"]["focus"],
                "ambiance": candidate["ranking"]["ambiance"],
            }
            for candidate in candidates
        ],
        "ranked_ordered_pairs": reduction["ranked_pairs"],
    })
    _write_json(out_dir / "05-reduction.json", {
        **{key: value for key, value in reduction.items() if key != "ranked_pairs"},
        "disagreement": disagreement,
    })
    e5_render.render_reduction(
        pal, images, reduction, candidates, unavailable,
        out_dir / "06-two-colour-sheet.jpg",
    )

    manifest = {
        "id": pal,
        "status": "ok",
        "experiment": EXPERIMENT,
        "space": SPACE,
        "created": datetime.now(timezone.utc).isoformat(),
        "source": _source(entry, image_path),
        "inputs": {
            "e4_experiment": E4_EXPERIMENT,
            "e4_status": e4_manifest["status"],
            "corpus_source": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
        },
        "models": {"segmenter": sam_name, "device": device},
        "settings": {
            "working_size": E5.WORKING,
            "semantic_replay": "frozen E4 successful phrase plus frozen E4 mask index",
            "hierarchy_recovery": recovery_report["method"],
            "candidate_sources": ["semantic", "hierarchy"],
            "output_roles": ["focus", "ambiance"],
            "qwen_reused": False,
            "hierarchy_rerun": False,
        },
        "candidate_counts": {
            "measured": len(candidates),
            "semantic": len(semantic),
            "hierarchy": len(hierarchy_pool),
            "retained_unavailable": len(unavailable),
            "ordered_pairs": len(reduction["ranked_pairs"]),
        },
        "selection": {
            "focus": reduction["roles"]["focus"],
            "ambiance": reduction["roles"]["ambiance"],
            "pair_metrics": reduction["pair_metrics"],
            "pair_score": reduction["pair_score"],
        },
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    return manifest


def _summary_record(manifest: dict) -> dict:
    selection = manifest["selection"]
    return {
        "id": manifest["id"],
        "status": manifest["status"],
        "focus": {
            key: selection["focus"][key]
            for key in ("candidate_id", "source", "hex", "rgb")
        },
        "ambiance": {
            key: selection["ambiance"][key]
            for key in ("candidate_id", "source", "hex", "rgb")
        },
        "pair_metrics": selection["pair_metrics"],
        "candidate_counts": manifest["candidate_counts"],
    }


def run(project_path: str, *, pilot: list[str] | None = None) -> Path:
    """Run E5 over precisely the frozen E4 nine-image pilot by default."""
    from services.silhouette import load_sam_model

    project = Path(project_path)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    e4_root = project / "outputs" / "tests" / SPACE / E4_EXPERIMENT
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    corpus = {
        entry["id"]: entry
        for entry in _read_json(corpus_dir / "corpus-manifest.json")["images"]
    }
    selected = list(pilot or PILOT)
    unexpected = set(selected) - set(PILOT)
    if unexpected:
        raise ValueError(f"E5 is restricted to the E4 pilot: {sorted(unexpected)}")
    missing = [pal for pal in selected if pal not in corpus]
    if missing:
        raise LookupError(f"Pilot records absent from frozen corpus: {missing}")
    out_root.mkdir(parents=True, exist_ok=True)

    print("loading SAM3 once for frozen phrase replay …")
    segmenter, sam_name, device = load_sam_model(str(project), "sam3")
    results = []
    try:
        for pal in selected:
            entry = corpus[pal]
            print(f"\n=== {pal}  {entry['film'][:32]}  ({entry['image_kind']})")
            try:
                results.append(run_one(
                    pal, entry, project, corpus_dir, e4_root, out_root,
                    segmenter, sam_name, device,
                ))
            except Exception as exc:
                print(f"  !! {type(exc).__name__}: {exc}")
                results.append({
                    "id": pal,
                    "status": "runner_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-1200:],
                })
    finally:
        del segmenter
        _free_cuda()

    succeeded = [result for result in results if result.get("status") == "ok"]
    summary = {
        "space": SPACE,
        "experiment": EXPERIMENT,
        "created": datetime.now(timezone.utc).isoformat(),
        "scope": "frozen E4 nine-image pilot only",
        "images": selected,
        "completed": len(succeeded),
        "failed": len(results) - len(succeeded),
        "results": [
            _summary_record(result) if result.get("status") == "ok" else result
            for result in results
        ],
        "method_note": (
            "No Qwen or E4 hierarchy was rerun. Semantic candidates use phrase replay "
            "against frozen E4 mask indexes; hierarchy candidates are recovered from "
            "unique exact-colour E4 hierarchy snapshots."
        ),
    }
    _write_json(out_root / "summary.json", summary)
    print(f"\nE5 complete -> {out_root}")
    return out_root


def main() -> Path:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="Crossing project containing the frozen E4 pilot")
    args = parser.parse_args()
    project = args.project
    if not project:
        from tool import prefs

        project = prefs.get("path")
    if not project:
        raise SystemExit("No project path configured. Pass --project.")
    return run(project)


if __name__ == "__main__":
    main()