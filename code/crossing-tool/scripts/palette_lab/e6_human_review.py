"""E6 — build the human-review apparatus and compare human answers to E5.

    uv run python -m scripts.palette_lab.e6_human_review build   --project /path
    uv run python -m scripts.palette_lab.e6_human_review compare --project /path
    uv run python -m scripts.palette_lab.e6_human_review census  --project /path

``build`` replays the frozen E4 phrases exactly as E5 did, so the reviewer sees
the same measured pixels, and verifies that rebuild against the frozen E5
inventory.  ``compare`` is model-free and reads the reviewer's answers back
against frozen E5 artifacts.  ``census`` records where each candidate sits in
the frozen E5 ranking, for use *after* review.  None of the three modes writes
outside ``E6-human-review/``.
"""

from __future__ import annotations

import argparse
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from scripts.palette_lab import e6_render
from scripts.palette_lab import e6_review as E6
from scripts.palette_lab import render as R
from scripts.palette_lab import stages
from scripts.palette_lab import two_color as E5
from scripts.palette_lab.e4_pilot import CORPUS_SPACE, PILOT, SPACE
from scripts.palette_lab.e5_two_color import _free_cuda, _read_json, _write_json


EXPERIMENT = "E6-human-review"
E4_EXPERIMENT = "E4-pilot"
E5_EXPERIMENT = "E5-two-color"

PROTOCOL = """# E6 Review Protocol

A reproducible protocol for reducing each frozen pilot frame to exactly two
measured colours by human judgment.

## What you are being asked

For each frame you get a blind candidate sheet: the original frame, then every
measured candidate colour shown painted over its own pixels. Candidates appear
in inventory order. **Nothing on the sheet tells you what E5 selected, or how it
ranked anything.** That is deliberate.

Answer four questions per frame.

### Question 1 — Focus

Which candidate colour best represents what you want the viewer to attend to in
this image?

This is *not* "which is most saturated", "which is largest", or "which is the
most important object". Use visual judgment.

### Question 2 — Ambiance

Which candidate colour best represents the larger chromatic field or atmosphere
in which that focus exists?

This is not necessarily the background.

### Question 3 — Pair

Taken together, do these two colours preserve the visual organization of the
frame? Answer `strong`, `adequate`, or `weak`.

### Question 4 — Why?

Short free text. This matters more than the choice itself: it may name a concept
the current model has no representation for.

## Candidate pool sufficiency

You are not required to accept the pool. For every frame, mark the pool
`sufficient` or `insufficient`.

If insufficient, say why. Suggested reasons, which are **not** exhaustive:

- `element_never_segmented`
- `relational_event_has_no_single_candidate`
- `measured_colour_contaminated_by_another_region`
- `small_accent_merged_away`
- `colour_only_present_as_unspatialized_reading`
- `two_colours_must_be_read_relationally`
- `no_candidate_expresses_the_visual_event`

Add your own in `candidate_pool.notes`. If you mark a pool insufficient you may
still record the nearest available candidates; both are preserved.

This separation is the point of E6. "E5 chose wrongly from an adequate pool" and
"the pool cannot express the right answer" are different failures and are never
merged in the comparison output.

## Review order

Review the five diagnostic frames first, in this order:

1. `PAL-001` — semantic convergence on a bright aperture.
2. `PAL-004` — a ~0.08% red accent.
3. `PAL-015` — a ~17% red accent that survives to the coarsest scale.
4. `PAL-022` — a ~2% red accent, between the other two.
5. `PAL-019` — control: strong red/black graphic structure.

Then `PAL-005`, `PAL-017`, `PAL-020`, `PAL-030`.

Do not read the E5 results, the E5 sheets, `pool-census.json`, `comparison.json`,
or `observations.md` before finishing at least the five diagnostic frames. Each
of those contains the E5 ranking you are being asked to judge independently.

## Recording answers

Copy `human-selections.template.json` to `human-selections.json`, set
`reviewer` to your identifier on every record you answer, fill `focus` and
`ambiance` with the `C##` review ids from the sheet, set `reviewed` to `true`,
and delete the records you did not review. Multiple reviewers append their own
records for the same image; answers are never merged.

## Status of these answers

Human selections are **evidence about what humans try to preserve**, not ground
truth and not a training target. E6 does not retune E5, and E5 is not modified
by anything recorded here.
"""


def _e6_candidates(pal: str, entry: dict, project: Path, segmenter) -> tuple[dict, list[dict], dict]:
    """Rebuild the E5 measured pool for one image, without any E5 scoring."""
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    e4_dir = project / "outputs" / "tests" / SPACE / E4_EXPERIMENT / pal
    hierarchy = _read_json(e4_dir / "04-hierarchy.json")
    inventory = _read_json(e4_dir / "07-inventory.json")
    residual = _read_json(e4_dir / "06-semantic-residual.json")
    images = stages.prepare_images(corpus_dir / entry["corpus_image"])

    recovered_masks, recovery = E5.recover_hierarchy_masks(hierarchy, e4_dir)
    replayed, replay = E5.replay_e4_phrases(segmenter, images["working"], inventory)
    semantic, unavailable = E5.semantic_candidates(inventory, replayed, images["working_rgb"])
    candidates = semantic + E5.hierarchy_candidates(hierarchy, recovered_masks, residual)
    provenance = {
        "semantic_phrase_replay": replay,
        "hierarchy_snapshot_recovery": recovery,
        "unavailable_e4_evidence": unavailable,
    }
    return images, candidates, provenance


def build_one(pal: str, entry: dict, project: Path, out_root: Path, segmenter) -> dict:
    """Write the blind review packet for one frozen pilot image."""
    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    e5_dir = project / "outputs" / "tests" / SPACE / E5_EXPERIMENT / pal

    images, candidates, provenance = _e6_candidates(pal, entry, project, segmenter)
    records = E6.build_review_records(candidates)
    verification = E6.verify_against_frozen_e5(
        candidates, _read_json(e5_dir / "03-candidate-inventory.json")
    )
    masks = {candidate["candidate_id"]: candidate["_mask"] for candidate in candidates}

    R.save_source(images, out_dir / "01-source.jpg")
    sheets = e6_render.render_candidate_sheets(pal, images, records, masks, out_dir)
    np.savez_compressed(
        out_dir / "04-candidate-masks.npz",
        **{key: value.astype(bool) for key, value in masks.items()},
    )
    _write_json(out_dir / "03-candidates.json", {
        "id": pal,
        "presentation_order": "frozen E5 inventory order; not E5 rank order",
        "candidates": records,
        "retained_unavailable_evidence": provenance["unavailable_e4_evidence"],
    })

    phase = "diagnostic" if pal in E6.DIAGNOSTIC_ORDER else "remaining"
    manifest = {
        "id": pal,
        "status": "ok",
        "experiment": EXPERIMENT,
        "space": SPACE,
        "phase": phase,
        "review_position": E6.review_order().index(pal) + 1,
        "created": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "e4_experiment": E4_EXPERIMENT,
            "e5_experiment": E5_EXPERIMENT,
            "corpus_source": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
        },
        "apparatus": {
            "blind": True,
            "blind_note": "No E5 score, rank, or selection appears on the sheets.",
            "candidate_sheets": sheets,
            "candidate_count": len(records),
            "semantic_candidates": sum(1 for r in records if r["source"] == "semantic"),
            "hierarchy_candidates": sum(1 for r in records if r["source"] == "hierarchy"),
            "retained_unavailable": len(provenance["unavailable_e4_evidence"]),
        },
        "pool_verification": verification,
        "replay_provenance": {
            "semantic_phrase_replay": provenance["semantic_phrase_replay"],
            "hierarchy_snapshot_recovery": provenance["hierarchy_snapshot_recovery"],
        },
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    return manifest


def build(project_path: str, *, pilot: list[str] | None = None) -> Path:
    """Build the apparatus for the frozen nine-image pilot, diagnostics first."""
    from services.silhouette import load_sam_model

    project = Path(project_path)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    corpus = {
        entry["id"]: entry
        for entry in _read_json(corpus_dir / "corpus-manifest.json")["images"]
    }
    selected = list(pilot or E6.review_order())
    unexpected = set(selected) - set(PILOT)
    if unexpected:
        raise ValueError(f"E6 is restricted to the E4/E5 pilot: {sorted(unexpected)}")
    out_root.mkdir(parents=True, exist_ok=True)

    segmenter, sam_name, device = load_sam_model(str(project), "sam3")
    manifests, failures = [], []
    try:
        for pal in selected:
            print(f"[E6] {pal}", flush=True)
            try:
                manifests.append(build_one(pal, corpus[pal], project, out_root, segmenter))
            except Exception as exc:
                failures.append({"id": pal, "error": f"{type(exc).__name__}: {exc}",
                                 "traceback": traceback.format_exc()[-1200:]})
                print(f"[E6] {pal} FAILED: {exc}", flush=True)
    finally:
        del segmenter
        _free_cuda()

    (out_root / "review-protocol.md").write_text(PROTOCOL, encoding="utf-8")
    template = []
    for manifest in manifests:
        records = _read_json(out_root / manifest["id"] / "03-candidates.json")["candidates"]
        template.append(E6.selection_template(manifest["id"], records, manifest["phase"]))
    _write_json(out_root / "human-selections.template.json", {
        "experiment": EXPERIMENT,
        "protocol": "review-protocol.md",
        "status": "template; copy to human-selections.json and fill in",
        "reviewers": [],
        "records": template,
    })
    _write_json(out_root / "build-report.json", {
        "experiment": EXPERIMENT,
        "created": datetime.now(timezone.utc).isoformat(),
        "models": {"segmenter": sam_name, "device": device},
        "review_order": E6.review_order(),
        "built": len(manifests),
        "failed": failures,
        "pool_verification": {
            manifest["id"]: {
                "identical_pool": manifest["pool_verification"]["identical_pool"],
                "matched": manifest["pool_verification"]["matched"],
                "frozen_candidates": manifest["pool_verification"]["frozen_candidates"],
                "missing_from_rebuild": manifest["pool_verification"]["missing_from_rebuild"],
                "absent_from_e5": manifest["pool_verification"]["absent_from_e5"],
                "measurement_drift": len(manifest["pool_verification"]["measurement_drift"]),
            }
            for manifest in manifests
        },
        "candidate_counts": {
            manifest["id"]: manifest["apparatus"]["candidate_count"] for manifest in manifests
        },
    })
    print(f"[E6] built {len(manifests)} records, {len(failures)} failed -> {out_root}")
    return out_root


def compare(project_path: str, *, selections_name: str = "human-selections.json") -> Path:
    """Compare recorded human selections against frozen E5. No models needed."""
    project = Path(project_path)
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    e5_root = project / "outputs" / "tests" / SPACE / E5_EXPERIMENT
    selections_path = out_root / selections_name
    if not selections_path.exists():
        raise FileNotFoundError(
            f"No reviewer answers at {selections_path}. Copy "
            f"human-selections.template.json, fill it in, and rerun."
        )
    payload = _read_json(selections_path)
    comparisons = []
    for selection in payload.get("records", []):
        if not selection.get("reviewed"):
            continue
        pal = selection["id"]
        records = _read_json(out_root / pal / "03-candidates.json")["candidates"]
        masks_path = out_root / pal / "04-candidate-masks.npz"
        masks = E6.load_masks(masks_path) if masks_path.exists() else None
        comparisons.append(E6.compare_one(
            selection,
            records,
            _read_json(e5_root / pal / "05-reduction.json"),
            _read_json(e5_root / pal / "04-pair-ranking.json"),
            masks,
        ))
    result = {
        "experiment": EXPERIMENT,
        "created": datetime.now(timezone.utc).isoformat(),
        "compared_against": f"{SPACE}/{E5_EXPERIMENT}",
        "status_note": (
            "Human selections are evidence, not ground truth. E5 is unmodified."
        ),
        "summary": E6.summarise(comparisons),
        "comparisons": comparisons,
    }
    _write_json(out_root / "comparison.json", result)
    print(f"[E6] compared {len(comparisons)} reviewed records -> "
          f"{out_root / 'comparison.json'}")
    return out_root / "comparison.json"


def census(project_path: str) -> Path:
    """Record where each measured candidate sits in frozen E5. No models needed."""
    project = Path(project_path)
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    e5_root = project / "outputs" / "tests" / SPACE / E5_EXPERIMENT
    images = {}
    for pal in E6.review_order():
        candidates_path = out_root / pal / "03-candidates.json"
        if not candidates_path.exists():
            continue
        images[pal] = E6.census(
            _read_json(candidates_path)["candidates"],
            _read_json(e5_root / pal / "04-pair-ranking.json"),
            _read_json(e5_root / pal / "05-reduction.json"),
        )
    path = out_root / "pool-census.json"
    _write_json(path, {
        "experiment": EXPERIMENT,
        "created": datetime.now(timezone.utc).isoformat(),
        "source": f"{SPACE}/{E5_EXPERIMENT} (read-only)",
        "do_not_open_before_review": (
            "This file contains the frozen E5 ranking. Reading it before "
            "reviewing a frame would anchor the reviewer to the baseline."
        ),
        "images": images,
    })
    print(f"[E6] census over {len(images)} images -> {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="E6 human-reviewed two-colour study")
    parser.add_argument("mode", choices=("build", "compare", "census"))
    parser.add_argument("--project", required=True, help="Crossing project root")
    parser.add_argument("--only", nargs="*", help="Restrict build to these pilot ids")
    parser.add_argument("--selections", default="human-selections.json",
                        help="Reviewer answer file inside the E6 directory")
    args = parser.parse_args()
    if args.mode == "build":
        build(args.project, pilot=args.only or None)
    elif args.mode == "census":
        census(args.project)
    else:
        compare(args.project, selections_name=args.selections)


if __name__ == "__main__":
    main()
