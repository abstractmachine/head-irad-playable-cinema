"""E7 — relational figure/ground palette experiment.

    uv run python -m scripts.palette_lab.e7_relational assess   --project /path
    uv run python -m scripts.palette_lab.e7_relational run      --project /path
    uv run python -m scripts.palette_lab.e7_relational evaluate --project /path

E7 is model-free. It reads frozen E5 and E6 artifacts read-only and writes only
beneath ``E7-relational-figure-ground/``. It does not modify production
``data/palette.py`` or any of E1-E6.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from scripts.palette_lab import e7_render
from scripts.palette_lab import relational as E7
from scripts.palette_lab import stages
from scripts.palette_lab.e4_pilot import CORPUS_SPACE, SPACE
from scripts.palette_lab.e5_two_color import _read_json, _write_json
from scripts.palette_lab.e6_review import review_order


EXPERIMENT = "E7-relational-figure-ground"
E5_EXPERIMENT = "E5-two-color"
E6_EXPERIMENT = "E6-human-review"

TOP_N = 10

# Read off the E7 brief's prose summary of Douglas's E6 review. No machine
# readable human-selections.json was ever recorded, so this is an approximate
# restatement kept deliberately as label sets, never as a target to fit.
HUMAN_REFERENCE = {
    "PAL-001": {"described": "desert / cave interior",
                "figure": ["desert plain", "desert landscape", "desert terrain", "cave entrance"],
                "ground": ["cave interior"]},
    "PAL-004": {"described": "woman or entering man / cabin interior",
                "figure": ["woman in blue dress", "man in top hat"],
                "ground": ["cabin interior"]},
    "PAL-005": {"described": "woman in red cloak / snowy landscape",
                "figure": ["woman in red cloak"],
                "ground": ["snowy landscape", "snow-covered ground", "snow and ice"]},
    "PAL-015": {"described": "man in red coat / snow",
                "figure": ["man in red coat", "red coat"],
                "ground": ["snowy ground", "snow-covered ground"]},
    "PAL-017": {"described": "revolver / background buildings",
                "figure": ["revolver", "revolver barrel", "hand holding revolver"],
                "ground": ["background buildings"]},
    "PAL-019": {"described": "cowboy silhouette / red background",
                "figure": ["silhouette of cowboy"],
                "ground": ["red background"]},
    "PAL-020": {"described": "human figure / contrasting environmental field",
                "figure": ["silhouetted figure", "dark silhouettes"],
                "ground": ["dusk sky", "orange sky", "bright horizon"]},
    "PAL-022": {"described": "faces / human action / snowy forest",
                "figure": ["central figure's face"],
                "ground": ["snowy forest background", "snowy background",
                           "winter landscape", "surrounding environment"]},
    "PAL-030": {"described": "man holding revolver / fiery background",
                "figure": ["man holding revolver", "man with revolver"],
                "ground": ["fiery background", "fiery sky", "red-orange sky"]},
}


def _paths(project: Path) -> tuple[Path, Path, Path, Path]:
    tests = project / "outputs" / "tests"
    return (
        tests / CORPUS_SPACE / "corpus",
        tests / SPACE / E5_EXPERIMENT,
        tests / SPACE / E6_EXPERIMENT,
        tests / SPACE / EXPERIMENT,
    )


def _load_pool(e6_root: Path, pal: str) -> tuple[list[dict], dict[str, np.ndarray]]:
    records = _read_json(e6_root / pal / "03-candidates.json")["candidates"]
    with np.load(e6_root / pal / "04-candidate-masks.npz", allow_pickle=False) as archive:
        masks = {key: archive[key].astype(bool) for key in archive.files}
    return records, masks


def _cross_source(e5_root: Path, pal: str) -> dict[str, float]:
    inventory = _read_json(e5_root / pal / "03-candidate-inventory.json")
    return {
        candidate["candidate_id"]: float(
            candidate.get("cross_source", {}).get("best_overlap_iou", 0.0)
        )
        for candidate in inventory["measured_candidates"]
    }


def _working_rgb(corpus_dir: Path, corpus: dict, pal: str) -> np.ndarray:
    return stages.prepare_images(corpus_dir / corpus[pal]["corpus_image"])["working_rgb"]


def _corpus(corpus_dir: Path) -> dict:
    return {
        entry["id"]: entry
        for entry in _read_json(corpus_dir / "corpus-manifest.json")["images"]
    }


# ---------------------------------------------------------------------------
# assess — what can and cannot currently be measured
# ---------------------------------------------------------------------------

_DECLARED_EXTENT_RANGE = {"small": (0.0, 5.0), "medium": (5.0, 25.0), "large": (25.0, 100.0)}


def assess(project_path: str) -> Path:
    """Answer the pre-implementation questions from measurements, not guesses."""
    project = Path(project_path)
    corpus_dir, e5_root, e6_root, out_root = _paths(project)
    out_root.mkdir(parents=True, exist_ok=True)
    corpus = _corpus(corpus_dir)

    images, lens_phrases, extent_rows = {}, Counter(), []
    for pal in review_order():
        records, masks = _load_pool(e6_root, pal)
        arr_rgb = _working_rgb(corpus_dir, corpus, pal)
        E7.lens_support(records, masks)
        multiplicity = E7.colour_multiplicity(records)
        geometry = E7.prepare_geometry(records, masks)

        semantic = [record for record in records if record["source"] == "semantic"]
        for record in semantic:
            lens_phrases[record["semantic_role"]] += 1
            extent = record["declared_extent"]
            if extent in _DECLARED_EXTENT_RANGE:
                low, high = _DECLARED_EXTENT_RANGE[extent]
                extent_rows.append(low <= record["area_percent"] < high)

        ids = [record["candidate_id"] for record in records]
        contact = sum(
            1 for figure in ids for ground in ids
            if figure != ground
            and (geometry[figure]["contact_ring"] & geometry[ground]["mask"]).any()
        )
        neighbourhood = sum(
            1 for figure in ids for ground in ids
            if figure != ground
            and (geometry[figure]["ring"] & geometry[ground]["mask"]).any()
        )
        ordered = len(ids) * (len(ids) - 1)
        colours = Counter(record["hex"] for record in records)
        semantic_colours = Counter(record["hex"] for record in semantic)
        reference = HUMAN_REFERENCE[pal]
        labels = {record["label"] for record in semantic}

        images[pal] = {
            "candidates": len(records),
            "semantic": len(semantic),
            "distinct_masks": len({masks[i].tobytes() for i in ids}),
            "distinct_colours": len(colours),
            "distinct_semantic_colours": len(semantic_colours),
            "largest_semantic_colour_group": semantic_colours.most_common(1)[0]
            if semantic_colours else None,
            "max_colour_multiplicity": max(multiplicity.values()),
            "ordered_pairs": ordered,
            "pairs_in_contact": contact,
            "pairs_in_contact_fraction": round(contact / ordered, 4) if ordered else 0.0,
            "pairs_in_neighbourhood": neighbourhood,
            "pairs_in_neighbourhood_fraction": round(neighbourhood / ordered, 4)
            if ordered else 0.0,
            "working_rgb_reproduces_frozen_colours": _colour_check(records, masks, arr_rgb),
            "human_reference": {
                "described": reference["described"],
                "figure_labels_present": sorted(set(reference["figure"]) & labels),
                "figure_labels_missing": sorted(set(reference["figure"]) - labels),
                "ground_labels_present": sorted(set(reference["ground"]) & labels),
                "ground_labels_missing": sorted(set(reference["ground"]) - labels),
                "representation_gap": not (set(reference["ground"]) & labels)
                or not (set(reference["figure"]) & labels),
            },
        }

    consistent = sum(1 for row in extent_rows if row)
    assessment = {
        "experiment": EXPERIMENT,
        "created": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "candidate_pool": f"{SPACE}/{E6_EXPERIMENT} (frozen, read-only)",
            "cross_source_evidence": f"{SPACE}/{E5_EXPERIMENT} (frozen, read-only)",
            "models_used": "none",
        },
        "measurable_now": {
            "formal": list(E7.FORMAL_SIGNALS) + ["hue_relation (conditional)"],
            "spatial": list(E7.SPATIAL_SIGNALS) + [
                "reverse_enclosure", "contact_pixels", "centroid_distance",
                "overlap_iou", "ground_compactness",
            ],
            "semantic": list(E7.SEMANTIC_SIGNALS),
            "note": "All of these come from persisted masks, stored LAB values, "
                    "and the frozen E5 cross-source evidence.",
        },
        "requires_recomputation_from_masks": [
            "ring / neighbourhood dilation", "enclosure", "compactness",
            "contact", "local_delta_e (needs the deterministic working RGB)",
        ],
        "not_measurable": {
            "agent_action_setting_semantics": {
                "answer": "No.",
                "lens_distribution": dict(lens_phrases),
                "evidence": (
                    "The E4 lens does not separate agent from setting: the "
                    "narrative lens nominates 'desert plain', 'fiery background' "
                    "and 'red background'; the atmospheric lens nominates "
                    "'background buildings' and 'doorway'."
                ),
                "declared_extent_consistent_with_measured_coverage": {
                    "consistent": consistent,
                    "of": len(extent_rows),
                    "fraction": round(consistent / len(extent_rows), 4) if extent_rows else 0.0,
                },
                "consequence": (
                    "E7 must not build an agent/setting axis. Ground-likeness is "
                    "taken from measured spatial structure only."
                ),
            },
            "depth_or_occlusion": "No depth evidence exists anywhere in E1-E6.",
            "narrative_importance": "No candidate carries a narrative weight field.",
        },
        "duplication_handling": {
            "candidate_multiplicity": "count of candidates sharing an identical "
                                      "measured colour; recorded, never scored",
            "independent_agreement": "per-lens best mask IoU, summed over distinct "
                                     "lenses; continuous and unthresholded",
        },
        "parameters": {
            "ring_radius_px": E7.RING_RADIUS,
            "contact_radius_px": E7.CONTACT_RADIUS,
            "hue_min_chroma": E7.HUE_MIN_CHROMA,
            "working_grid": 256,
            "ring_radius_note": (
                "Figure/ground is treated as a neighbourhood relation because "
                "strict contact is too sparse: the human PAL-015 pair shares "
                "zero touching pixels."
            ),
        },
        "images": images,
    }
    path = out_root / "00-assessment.json"
    _write_json(path, assessment)
    print(f"[E7] assessment -> {path}")
    return path


def _colour_check(records: list[dict], masks: dict, arr_rgb: np.ndarray) -> bool:
    """Confirm the recomputed working grid still measures the frozen colours."""
    for record in records[:5]:
        mask = masks[record["candidate_id"]]
        pixels = arr_rgb[mask]
        rgb = np.rint(pixels.astype(np.float64).mean(axis=0)).astype(np.uint8)
        if rgb.tolist() != list(record["rgb"]):
            return False
    return True


# ---------------------------------------------------------------------------
# run — relational pair ranking
# ---------------------------------------------------------------------------

def _public_pair(pair: dict) -> dict:
    semantic = {
        key: value for key, value in pair["semantic"].items()
        if key not in ("figure_lens_support", "ground_lens_support")
    }
    return {**pair, "semantic": semantic}


def run_one(pal: str, corpus_dir: Path, corpus: dict, e5_root: Path, e6_root: Path,
            out_root: Path) -> dict:
    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    records, masks = _load_pool(e6_root, pal)
    arr_rgb = _working_rgb(corpus_dir, corpus, pal)
    scored = E7.build_pairs(records, masks, arr_rgb, _cross_source(e5_root, pal))
    by_id = {record["candidate_id"]: record for record in records}
    tops = {
        hypothesis: E7.distinct_colour_pairs(
            E7.rank_pairs(scored, hypothesis), by_id, TOP_N
        )
        for hypothesis in E7.HYPOTHESES
    }
    _write_json(out_dir / "01-candidate-evidence.json", {
        "id": pal,
        "note": "Per-candidate evidence shared by every pair it appears in.",
        "candidates": [
            {
                "review_id": record["review_id"],
                "candidate_id": record["candidate_id"],
                "source": record["source"],
                "label": record["label"],
                "hex": record["hex"],
                "area_percent": record["area_percent"],
                "lens_support": record.get("_lens_support") or {},
                "independent_lens_agreement": round(
                    sum((record.get("_lens_support") or {}).values()), 4
                ),
                "colour_multiplicity": record.get("_multiplicity"),
            }
            for record in records
        ],
    })
    _write_json(out_dir / "02-pair-evidence.json", {
        "id": pal,
        "ordered_pairs": len(scored),
        "note": "Complete relational evidence vector for every ordered pair.",
        "pairs": [_public_pair(pair) for pair in scored],
    })
    _write_json(out_dir / "03-hypothesis-ranking.json", {
        "id": pal,
        "presentation": "top pairs after collapsing identical measured colour "
                        "pairs; scores and the full ranking are unchanged",
        "hypotheses": {
            hypothesis: [
                {
                    "rank": pair["rank"],
                    "duplicate_records_collapsed": pair["represents"],
                    "figure_id": pair["figure_id"],
                    "ground_id": pair["ground_id"],
                    "figure_label": pair["semantic"]["figure_label"],
                    "ground_label": pair["semantic"]["ground_label"],
                    "figure_hex": by_id[pair["figure_id"]]["hex"],
                    "ground_hex": by_id[pair["ground_id"]]["hex"],
                    "score": pair["hypotheses"][hypothesis],
                    "families": {
                        family: pair["families"][family]["score"]
                        for family in ("formal", "spatial", "semantic")
                    },
                }
                for pair in tops[hypothesis]
            ]
            for hypothesis in E7.HYPOTHESES
        },
    })
    sheets = e7_render.render_relational_sheets(
        pal, corpus_dir / corpus[pal]["corpus_image"], by_id, masks, tops, out_dir
    )

    manifest = {
        "id": pal,
        "status": "ok",
        "experiment": EXPERIMENT,
        "space": SPACE,
        "created": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "candidate_pool": f"{E6_EXPERIMENT}/{pal}/03-candidates.json",
            "masks": f"{E6_EXPERIMENT}/{pal}/04-candidate-masks.npz",
            "cross_source": f"{E5_EXPERIMENT}/{pal}/03-candidate-inventory.json",
            "models_used": "none",
        },
        "unit_of_evaluation": "ordered pair (figure, ground)",
        "candidates": len(records),
        "ordered_pairs": len(scored),
        "parameters": {
            "ring_radius_px": E7.RING_RADIUS,
            "contact_radius_px": E7.CONTACT_RADIUS,
            "top_n": TOP_N,
        },
        "top_pair": {
            hypothesis: {
                "figure": tops[hypothesis][0]["semantic"]["figure_label"],
                "figure_hex": by_id[tops[hypothesis][0]["figure_id"]]["hex"],
                "ground": tops[hypothesis][0]["semantic"]["ground_label"],
                "ground_hex": by_id[tops[hypothesis][0]["ground_id"]]["hex"],
                "score": tops[hypothesis][0]["hypotheses"][hypothesis],
            }
            for hypothesis in E7.HYPOTHESES
        },
        "sheets": sheets,
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    return manifest


def run(project_path: str, *, only: list[str] | None = None) -> Path:
    project = Path(project_path)
    corpus_dir, e5_root, e6_root, out_root = _paths(project)
    out_root.mkdir(parents=True, exist_ok=True)
    corpus = _corpus(corpus_dir)
    manifests = []
    for pal in (only or review_order()):
        print(f"[E7] {pal}", flush=True)
        manifests.append(run_one(pal, corpus_dir, corpus, e5_root, e6_root, out_root))
    _write_json(out_root / "summary.json", {
        "experiment": EXPERIMENT,
        "created": datetime.now(timezone.utc).isoformat(),
        "scope": "frozen E4/E5/E6 nine-image pilot only",
        "models_used": "none",
        "hypotheses": list(E7.HYPOTHESES),
        "results": [
            {"id": manifest["id"], "ordered_pairs": manifest["ordered_pairs"],
             "top_pair": manifest["top_pair"]}
            for manifest in manifests
        ],
    })
    print(f"[E7] ran {len(manifests)} images -> {out_root}")
    return out_root


# ---------------------------------------------------------------------------
# evaluate — against frozen E5 and the human reference
# ---------------------------------------------------------------------------

def _resolve_reference(records: list[dict], labels: list[str]) -> list[str]:
    wanted = set(labels)
    return [record["candidate_id"] for record in records if record["label"] in wanted]


def evaluate_one(pal: str, e5_root: Path, e6_root: Path, out_root: Path) -> dict:
    records = _read_json(e6_root / pal / "03-candidates.json")["candidates"]
    by_id = {record["candidate_id"]: record for record in records}
    pairs = _read_json(out_root / pal / "02-pair-evidence.json")["pairs"]
    reduction = _read_json(e5_root / pal / "05-reduction.json")
    e5_focus = reduction["roles"]["focus"]["candidate_id"]
    e5_ambiance = reduction["roles"]["ambiance"]["candidate_id"]

    reference = HUMAN_REFERENCE[pal]
    figure_ids = _resolve_reference(records, reference["figure"])
    ground_ids = _resolve_reference(records, reference["ground"])

    result = {
        "id": pal,
        "human_reference": {
            "described": reference["described"],
            "provenance": (
                "approximate restatement of the E7 brief's prose summary of the "
                "E6 review; no machine-readable human-selections.json exists"
            ),
            "figure_candidates_resolved": len(figure_ids),
            "ground_candidates_resolved": len(ground_ids),
            "representation_gap": not figure_ids or not ground_ids,
            "gap_detail": {
                "figure_labels_unresolved": sorted(
                    set(reference["figure"]) - {by_id[i]["label"] for i in figure_ids}
                ),
                "ground_labels_unresolved": sorted(
                    set(reference["ground"]) - {by_id[i]["label"] for i in ground_ids}
                ),
            },
        },
        "e5_selection": {
            "focus": {"candidate_id": e5_focus, "label": by_id[e5_focus]["label"],
                      "hex": by_id[e5_focus]["hex"]},
            "ambiance": {"candidate_id": e5_ambiance, "label": by_id[e5_ambiance]["label"],
                         "hex": by_id[e5_ambiance]["hex"]},
        },
        "hypotheses": {},
    }
    for hypothesis in E7.HYPOTHESES:
        ordered = E7.rank_pairs(pairs, hypothesis)
        positions = {
            (pair["figure_id"], pair["ground_id"]): index
            for index, pair in enumerate(ordered, start=1)
        }
        best_human = None
        best_unordered = None
        for figure in figure_ids:
            for ground in ground_ids:
                position = positions.get((figure, ground))
                if position is not None and (best_human is None or position < best_human["rank"]):
                    best_human = {
                        "rank": position, "of": len(ordered),
                        "figure_id": figure, "ground_id": ground,
                        "figure_label": by_id[figure]["label"],
                        "ground_label": by_id[ground]["label"],
                        "figure_hex": by_id[figure]["hex"], "ground_hex": by_id[ground]["hex"],
                    }
                # The brief treats the relation as primary; orientation is a
                # separate claim, so it is measured separately.
                for oriented, swapped in (((figure, ground), False), ((ground, figure), True)):
                    place = positions.get(oriented)
                    if place is None:
                        continue
                    if best_unordered is None or place < best_unordered["rank"]:
                        best_unordered = {
                            "rank": place, "of": len(ordered),
                            "figure_id": oriented[0], "ground_id": oriented[1],
                            "figure_label": by_id[oriented[0]]["label"],
                            "ground_label": by_id[oriented[1]]["label"],
                            "orientation_matches_human": not swapped,
                        }
        top = ordered[0]
        top_colours = {by_id[top["figure_id"]]["hex"], by_id[top["ground_id"]]["hex"]}
        human_colours = (
            {best_human["figure_hex"], best_human["ground_hex"]} if best_human else set()
        )
        # Exact candidate identity is too strict: several lenses measure the same
        # region to within a couple of delta E. Report the distance instead.
        colour_distance = None
        if best_human:
            top_figure_lab = by_id[top["figure_id"]]["lab"]
            top_ground_lab = by_id[top["ground_id"]]["lab"]
            human_figure_lab = by_id[best_human["figure_id"]]["lab"]
            human_ground_lab = by_id[best_human["ground_id"]]["lab"]
            aligned = max(
                E7.delta_e(top_figure_lab, human_figure_lab),
                E7.delta_e(top_ground_lab, human_ground_lab),
            )
            swapped = max(
                E7.delta_e(top_figure_lab, human_ground_lab),
                E7.delta_e(top_ground_lab, human_figure_lab),
            )
            colour_distance = {
                "figure_delta_e": E7.delta_e(top_figure_lab, human_figure_lab),
                "ground_delta_e": E7.delta_e(top_ground_lab, human_ground_lab),
                "worst_delta_e_same_orientation": round(aligned, 3),
                "worst_delta_e_swapped_orientation": round(swapped, 3),
                "closer_when_swapped": swapped < aligned,
            }
        result["hypotheses"][hypothesis] = {
            "top_pair": {
                "figure_id": top["figure_id"], "ground_id": top["ground_id"],
                "figure_label": top["semantic"]["figure_label"],
                "ground_label": top["semantic"]["ground_label"],
                "figure_hex": by_id[top["figure_id"]]["hex"],
                "ground_hex": by_id[top["ground_id"]]["hex"],
                "score": top["hypotheses"][hypothesis],
                "families": {
                    family: top["families"][family]["score"]
                    for family in ("formal", "spatial", "semantic")
                },
            },
            "top_alternatives": [
                {
                    "rank": pair["rank"],
                    "duplicate_records_collapsed": pair["represents"],
                    "figure_label": pair["semantic"]["figure_label"],
                    "ground_label": pair["semantic"]["ground_label"],
                    "figure_hex": by_id[pair["figure_id"]]["hex"],
                    "ground_hex": by_id[pair["ground_id"]]["hex"],
                    "score": pair["hypotheses"][hypothesis],
                }
                for pair in E7.distinct_colour_pairs(ordered, by_id, TOP_N)[1:]
            ],
            "human_pair_best_rank": best_human,
            "human_pair_best_rank_either_orientation": best_unordered,
            "top_pair_is_human_colour_pair": bool(human_colours) and top_colours == human_colours,
            "top_pair_vs_human_colour_distance": colour_distance,
            "top_pair_orientation_matches_human": (
                bool(human_colours) and top_colours == human_colours
                and by_id[top["figure_id"]]["hex"] == best_human["figure_hex"]
            ),
            "e5_pair_as_ordered": {
                "focus_as_figure": positions.get((e5_focus, e5_ambiance)),
                "focus_as_ground": positions.get((e5_ambiance, e5_focus)),
                "of": len(ordered),
            },
        }
    return result


def evaluate(project_path: str) -> Path:
    project = Path(project_path)
    _, e5_root, e6_root, out_root = _paths(project)
    results = [
        evaluate_one(pal, e5_root, e6_root, out_root)
        for pal in review_order()
        if (out_root / pal / "02-pair-evidence.json").exists()
    ]
    summary = {}
    for hypothesis in E7.HYPOTHESES:
        rows = [row["hypotheses"][hypothesis] for row in results]
        ranks = [row["human_pair_best_rank"]["rank"] for row in rows if row["human_pair_best_rank"]]
        unordered = [
            row["human_pair_best_rank_either_orientation"]["rank"]
            for row in rows if row["human_pair_best_rank_either_orientation"]
        ]
        summary[hypothesis] = {
            "images_with_resolvable_human_pair": len(ranks),
            "human_pair_rank_1": sum(1 for rank in ranks if rank == 1),
            "human_pair_in_top_10": sum(1 for rank in ranks if rank <= 10),
            "human_pair_in_top_50": sum(1 for rank in ranks if rank <= 50),
            "median_human_pair_rank": int(np.median(ranks)) if ranks else None,
            "worst_human_pair_rank": max(ranks) if ranks else None,
            "either_orientation_in_top_10": sum(1 for rank in unordered if rank <= 10),
            "median_rank_either_orientation": int(np.median(unordered)) if unordered else None,
            "top_pair_is_human_colour_pair": sum(
                1 for row in rows if row["top_pair_is_human_colour_pair"]
            ),
            "top_pair_orientation_matches_human": sum(
                1 for row in rows if row["top_pair_orientation_matches_human"]
            ),
        }
    path = out_root / "evaluation.json"
    _write_json(path, {
        "experiment": EXPERIMENT,
        "created": datetime.now(timezone.utc).isoformat(),
        "status_note": (
            "The human reference is evidence about conceptual structure, not "
            "ground truth. E7 is not tuned to reproduce it."
        ),
        "summary": summary,
        "images": results,
    })
    print(f"[E7] evaluated {len(results)} images -> {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="E7 relational figure/ground experiment")
    parser.add_argument("mode", choices=("assess", "run", "evaluate"))
    parser.add_argument("--project", required=True, help="Crossing project root")
    parser.add_argument("--only", nargs="*", help="Restrict run to these pilot ids")
    args = parser.parse_args()
    if args.mode == "assess":
        assess(args.project)
    elif args.mode == "run":
        run(args.project, only=args.only or None)
    else:
        evaluate(args.project)


if __name__ == "__main__":
    main()
