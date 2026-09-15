"""Post-E7 consolidation: record the E6 review faithfully, then re-evaluate E7.

    uv run python -m scripts.palette_lab.e7_consolidate record   --project /path
    uv run python -m scripts.palette_lab.e7_consolidate evaluate --project /path

``record`` validates the transcribed review against the frozen E6 pool and
writes ``E6-human-review/human-selections.json`` plus the documented schema
extension. ``evaluate`` re-ranks the *already computed* E7 pair evidence against
that record and writes post-hoc comparison artifacts.

Nothing here regenerates or changes E7 pair evidence or scoring, and no E1-E7
algorithm or artifact is modified.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from scripts.palette_lab import human_review as HR
from scripts.palette_lab import relational as E7
from scripts.palette_lab.e4_pilot import SPACE
from scripts.palette_lab.e5_two_color import _read_json, _write_json
from scripts.palette_lab.e6_review import review_order


E6_EXPERIMENT = "E6-human-review"
E7_EXPERIMENT = "E7-relational-figure-ground"
CONSOLIDATION = "consolidation"

REVIEWER = "Douglas Edric Stanley"

RECURSIVE_SELECTION_VERBATIM = (
    "There is a sort of recursive process where I decided the foreground based "
    "on the background and then the background based on that foreground and "
    "vice versa."
)
RECURSIVE_SELECTION_PARAPHRASE = (
    "Pair selection was relational and iterative: the suitability of the figure "
    "depended on the selected ground, and the suitability of the ground was "
    "reconsidered in relation to the selected figure."
)

REVIEW_OBSERVATIONS = {
    "recursive_selection": {
        "verbatim": RECURSIVE_SELECTION_VERBATIM,
        "paraphrase": RECURSIVE_SELECTION_PARAPHRASE,
        "status": "principal motivation for the E7 pair-first formulation",
    },
    "reinforcing_dimensions": {
        "note": "Recorded as a human-review observation, not a scoring formula.",
        "formal": ["tonal contrast", "chromatic contrast"],
        "compositional_perceptual": ["figure", "ground", "field", "enclosure", "prominence"],
        "semantic_narrative": [
            "human acting", "object/action", "setting",
            "environmental condition", "gaze",
        ],
    },
    "unresolved_representation_problem": {
        "statement": "One palette colour need not correspond to one segmented region.",
        "evidence": {
            "PAL-004": "the perceived room/setting is distributed across C41 + C36 + C25 + C22",
            "PAL-022": "the perceived human-action figure is distributed across C09 + C10 + C11",
        },
        "consequence": "palette role and single segmentation mask must be distinguished",
        "status": "open; deliberately not solved in this consolidation",
    },
}

REVIEW = {
    "PAL-001": {
        "preferred": {
            "figure": {"mode": "single", "candidates": ["C03"], "stated_label": "desert"},
            "ground": {"mode": "alternative", "candidates": ["C10", "C11"],
                       "preferred": None, "stated_label": "cave interior"},
            "rationale": "The contrasting spaces are the strongest visual distinction "
                         "because of their graphical foreground/background contrast.",
        },
        "alternatives": [{
            "figure": {"mode": "single", "candidates": ["C01"], "stated_label": "man in cave"},
            "ground": {"mode": "single", "candidates": ["C03"], "stated_label": "desert"},
            "rationale": "The man is a human doing something and therefore provides "
                         "narrative figurehood.",
            "note": "An explicit alternative semantic/narrative reading, not a "
                    "correction of the preferred chromatic/compositional reading.",
        }],
        "candidate_pool": {"sufficiency": "sufficient", "reasons": [], "notes": ""},
    },
    "PAL-004": {
        "preferred": {
            "figure": {"mode": "single", "candidates": ["C09"],
                       "stated_label": "woman in blue dress"},
            "ground": {"mode": "compound", "candidates": ["C41", "C36", "C25", "C22"],
                       "stated_label": "the room itself / the setting"},
            "rationale": "The room itself is the setting, expressed collectively by "
                         "several regions. The woman is compositionally prominent, the "
                         "entering man is looking toward her, and she is the object of "
                         "his gaze.",
        },
        "alternatives": [{
            "figure": {"mode": "single", "candidates": ["C02"],
                       "stated_label": "man in red shirt"},
            "ground": {"mode": "compound", "candidates": ["C41", "C36", "C25", "C22"],
                       "stated_label": "the room itself / the setting"},
            "rationale": "The entering man as figure instead of the woman.",
        }],
        "candidate_pool": {
            "sufficiency": "pieces_present_grouping_absent",
            "reasons": ["perceptual_role_requires_compound_region"],
            "notes": "The pool contains the room's pieces but no single candidate "
                     "expresses the room as one setting.",
        },
        "discrepancies": [{
            "kind": "review_id_label_conflict",
            "review_id": "C02",
            "stated_label": "man in red shirt",
            "pool_label": "man in top hat",
            "resolution": "recorded as cited; not substituted",
            "note": "C07 carries the stated label and is the same region: delta E "
                    "0.860, mask IoU 0.9847. The two candidates are one man measured "
                    "under two phrases, so the cited id and the stated label point at "
                    "effectively the same region.",
        }],
    },
    "PAL-005": {
        "preferred": {
            "figure": {"mode": "single", "candidates": ["C02"],
                       "stated_label": "woman in red cloak"},
            "ground": {"mode": "single", "candidates": ["C14"],
                       "stated_label": "snowy landscape"},
            "rationale": "The snowy landscape is both the strongest visual/chromatic "
                         "field and the actual narrative setting. The woman is "
                         "chromatically distinct, positionally prominent, and the "
                         "central focus of the image.",
        },
        "alternatives": [],
        "candidate_pool": {"sufficiency": "sufficient", "reasons": [], "notes": ""},
    },
    "PAL-015": {
        "preferred": {
            "figure": {"mode": "single", "candidates": ["C02"],
                       "stated_label": "man in red coat"},
            "ground": {"mode": "single", "candidates": ["C09"],
                       "stated_label": "snowy ground"},
            "rationale": "The snow creates the strongest tonal/chromatic opposition "
                         "with the figure and is narratively meaningful as setting "
                         "(snow -> cold -> winter -> environmental condition). The "
                         "trees are comparatively incidental. The red-coated man is "
                         "figure because of strong colour, position, orientation, gaze "
                         "and gait.",
        },
        "alternatives": [],
        "candidate_pool": {"sufficiency": "sufficient", "reasons": [], "notes": ""},
    },
    "PAL-017": {
        "preferred": {
            "figure": {"mode": "single", "candidates": ["C02"],
                       "stated_label": "revolver barrel"},
            "ground": {"mode": "single", "candidates": ["C12"],
                       "stated_label": "background buildings"},
            "rationale": "The buildings function as actual setting. The revolver is the "
                         "strongest narrative element and is strongly tonally and "
                         "chromatically distinct from that setting.",
            "note": "The reviewer explicitly compared this composition to PAL-001.",
        },
        "alternatives": [],
        "candidate_pool": {"sufficiency": "sufficient", "reasons": [], "notes": ""},
    },
    "PAL-019": {
        "preferred": {
            "figure": {"mode": "single", "candidates": ["C01"],
                       "stated_label": "silhouette of cowboy"},
            "ground": {"mode": "single", "candidates": ["C02"],
                       "stated_label": "red background"},
            "rationale": "The simplest case: strong tonal contrast, strong chromatic "
                         "contrast, strong semantic distinction, strong narrative "
                         "distinction.",
        },
        "alternatives": [],
        "candidate_pool": {"sufficiency": "sufficient", "reasons": [], "notes": ""},
    },
    "PAL-020": {
        "preferred": {
            "figure": {"mode": "single", "candidates": ["C03"],
                       "stated_label": "human figure"},
            "ground": {"mode": "alternative", "candidates": ["C11", "C27"],
                       "preferred": "C11",
                       "stated_label": "contrasting environmental field"},
            "rationale": "The ground is selected because of its tonal/chromatic "
                         "relation to the human figure. The human figure is foreground "
                         "because it is a person acting or doing something, while "
                         "retaining enough visual contrast from the field.",
        },
        "alternatives": [],
        "candidate_pool": {"sufficiency": "sufficient", "reasons": [], "notes": ""},
        "discrepancies": [{
            "kind": "cited_candidate_is_a_small_instance",
            "review_id": "C03",
            "stated_label": "human figure",
            "pool_label": "silhouetted figure",
            "resolution": "recorded as cited; not substituted",
            "note": "C03 measures 0.038% of the frame. Larger candidates carrying the "
                    "same phrase exist (C04 at 4.024%, C05 at 3.882%), but C05 measures "
                    "#010004, the same colour as the selected ground, so the cited "
                    "small instance is the one that preserves contrast with the field.",
        }],
    },
    "PAL-022": {
        "preferred": {
            "figure": {"mode": "compound", "candidates": ["C09", "C10", "C11"],
                       "stated_label": "faces / human action"},
            "ground": {"mode": "single", "candidates": ["C01"],
                       "stated_label": "snowy forest background"},
            "rationale": "The ground is straightforward: background, chromatic "
                         "opposition, narrative setting. The figure is the human "
                         "action and the faces together; no single candidate "
                         "adequately represents it.",
        },
        "alternatives": [],
        "candidate_pool": {
            "sufficiency": "pieces_present_grouping_absent",
            "reasons": ["perceptual_role_requires_compound_region"],
            "notes": "Several face and action pieces are present, but no single "
                     "candidate represents the perceived human-action figure.",
        },
    },
    "PAL-030": {
        "preferred": {
            "figure": {"mode": "single", "candidates": ["C01"],
                       "stated_label": "man holding revolver"},
            "ground": {"mode": "single", "candidates": ["C03"],
                       "stated_label": "fiery background"},
            "rationale": "The figure is a human performing an action. The fiery field "
                         "provides the contrasting ground.",
        },
        "alternatives": [],
        "candidate_pool": {"sufficiency": "sufficient", "reasons": [], "notes": ""},
    },
}

EXTENSION_DOC = """# E6 Review Schema Extension

`review-protocol.md` remains the protocol that was actually administered. This
file documents the **additive** schema extension needed to record the review
faithfully, and is written by
`scripts/palette_lab/e7_consolidate.py record`.

## Why the original schema was not sufficient

The original `human-selections.template.json` record has:

```json
"focus":    {"review_id": "", "candidate_id": ""},
"ambiance": {"review_id": "", "candidate_id": ""}
```

That assumes exactly one candidate per role. The review did not always work that
way, and three of the reviewer's states are not interchangeable:

| State | Meaning | Example |
| --- | --- | --- |
| `single` | one candidate | PAL-015 figure = `C02` |
| `alternative` | a choice between candidates, any acceptable | PAL-020 ground = `C11` **or** `C27` |
| `compound` | several regions acting as one role | PAL-004 ground = `C41` **+** `C36` **+** `C25` **+** `C22` |

Representing `alternative` and `compound` as one flat list would erase the
difference between OR and AND, so each role carries an explicit `mode`.

## Extension

Each record replaces the flat `focus`/`ambiance` pair with:

- `role_vocabulary` — `"figure_ground"`. The review was expressed as
  figure/ground, not as E5/E6's focus/ambiance. The two are not assumed
  equivalent.
- `preferred` — `{figure, ground, rationale, note}`; the reading the reviewer
  preferred.
- `alternatives` — a list of whole competing readings, each with the same shape.
  PAL-001 has one: a narrative reading that inverts the preferred one.
- Each role is `{mode, candidates, preferred, stated_label, resolved}` where
  `mode` is one of `single`, `alternative`, `compound`; `preferred` names the
  favoured member of an `alternative` role or is `null` when none was stated;
  and `resolved` carries the frozen candidate id, label, hex and area for every
  cited review id so the record is self-describing.
- `review_notes` — includes the reviewer's recursive-selection observation,
  verbatim and paraphrased.
- `discrepancies` — places where a cited review id and its stated label disagree.
  These are recorded, never silently corrected.

`id`, `phase`, `reviewer`, `reviewed`, `pair_assessment`, `rationale`,
`candidate_pool` and `notes` keep their original meaning.

## Sufficiency vocabulary extension

The original protocol offered `sufficient` and `insufficient`. The review needs a
third state, because a pool can hold all the right material and still not express
a perceived role:

- `pieces_present_grouping_absent` — the material is present, but the perceptual
  grouping the reviewer saw has no single candidate.

with a matching reason:

- `perceptual_role_requires_compound_region`

PAL-004 and PAL-022 are recorded in this state. This is deliberately distinct
from `insufficient`, which means the visual material itself is missing or
unusable.

## What this extension does not do

It does not resolve ambiguity, rank the alternatives, or convert a compound role
into a representative candidate. Where the reviewer gave two readings, both are
kept; where a role is compound, it stays compound and downstream comparison
reports `representation_mismatch` rather than choosing a member.
"""


def _paths(project: Path) -> tuple[Path, Path]:
    tests = project / "outputs" / "tests" / SPACE
    return tests / E6_EXPERIMENT, tests / E7_EXPERIMENT


def _pool(e6_root: Path, pal: str) -> tuple[dict, dict]:
    records = _read_json(e6_root / pal / "03-candidates.json")["candidates"]
    by_review = {record["review_id"]: record for record in records}
    by_candidate = {record["candidate_id"]: record for record in records}
    return by_review, by_candidate


def _resolved(role: dict, by_review: dict) -> list[dict]:
    return [
        {
            "review_id": review_id,
            "candidate_id": by_review[review_id]["candidate_id"],
            "pool_label": by_review[review_id]["label"],
            "source": by_review[review_id]["source"],
            "hex": by_review[review_id]["hex"],
            "area_percent": by_review[review_id]["area_percent"],
        }
        for review_id in role.get("candidates") or []
        if review_id in by_review
    ]


def _role_record(role: dict, by_review: dict) -> dict:
    return {
        "mode": role["mode"],
        "candidates": list(role["candidates"]),
        "preferred": role.get("preferred"),
        "stated_label": role.get("stated_label", ""),
        "resolved": _resolved(role, by_review),
    }


def record(project_path: str) -> Path:
    """Validate and write the faithful machine-readable review."""
    project = Path(project_path)
    e6_root, _ = _paths(project)
    records, problems = [], []
    for pal in review_order():
        by_review, _ = _pool(e6_root, pal)
        source = REVIEW[pal]
        entry = {
            "id": pal,
            "phase": _read_json(e6_root / pal / "00-manifest.json")["phase"],
            "reviewer": REVIEWER,
            "reviewed": True,
            "role_vocabulary": HR.ROLE_VOCABULARY,
            "preferred": {
                "figure": _role_record(source["preferred"]["figure"], by_review),
                "ground": _role_record(source["preferred"]["ground"], by_review),
                "rationale": source["preferred"].get("rationale", ""),
                "note": source["preferred"].get("note", ""),
            },
            "alternatives": [
                {
                    "figure": _role_record(alternative["figure"], by_review),
                    "ground": _role_record(alternative["ground"], by_review),
                    "rationale": alternative.get("rationale", ""),
                    "note": alternative.get("note", ""),
                }
                for alternative in source.get("alternatives") or []
            ],
            "pair_assessment": None,
            "rationale": source["preferred"].get("rationale", ""),
            "candidate_pool": source["candidate_pool"],
            "discrepancies": source.get("discrepancies", []),
            "review_notes": {
                "recursive_selection": True,
                "verbatim": RECURSIVE_SELECTION_VERBATIM,
            },
            "notes": "",
        }
        problems.extend(HR.validate_record(entry, set(by_review)))
        records.append(entry)

    if problems:
        raise ValueError("Review transcription does not match the frozen E6 pool:\n  "
                         + "\n  ".join(problems))

    path = e6_root / "human-selections.json"
    _write_json(path, {
        "experiment": E6_EXPERIMENT,
        "created": datetime.now(timezone.utc).isoformat(),
        "protocol": "review-protocol.md",
        "schema_extension": "review-protocol-extension.md",
        "reviewers": [REVIEWER],
        "reviewer_count": 1,
        "single_reviewer_note": (
            "One reviewer contributed. Recorded explicitly; a single reviewer's "
            "reading is evidence about perception, not ground truth."
        ),
        "status_note": (
            "Transcribed from the reviewer's written review during post-E7 "
            "consolidation. Ambiguity, alternatives and compound roles are "
            "preserved rather than resolved."
        ),
        "observations": REVIEW_OBSERVATIONS,
        "records": records,
    })
    (e6_root / "review-protocol-extension.md").write_text(EXTENSION_DOC, encoding="utf-8")
    print(f"[consolidate] recorded {len(records)} reviewed images -> {path}")
    return path


def evaluate_one(pal: str, e6_root: Path, e7_root: Path, entry: dict) -> dict:
    """Locate every stated reading inside the already-computed E7 ranking."""
    by_review, by_candidate = _pool(e6_root, pal)
    pairs = _read_json(e7_root / pal / "02-pair-evidence.json")["pairs"]
    review_to_candidate = {
        review_id: record["candidate_id"] for review_id, record in by_review.items()
    }
    result = {
        "id": pal,
        "reviewer": entry["reviewer"],
        "candidate_pool": entry["candidate_pool"],
        "discrepancies": entry.get("discrepancies", []),
        "readings": [],
    }
    for reading in HR.readings(entry):
        row = {
            "kind": reading["kind"],
            "index": reading["index"],
            "comparison": HR.reading_kind(reading),
            "figure_mode": reading["figure"]["mode"],
            "ground_mode": reading["ground"]["mode"],
            "figure_stated_label": reading["figure"].get("stated_label", ""),
            "ground_stated_label": reading["ground"].get("stated_label", ""),
            "rationale": reading.get("rationale", ""),
            "per_hypothesis": {},
        }
        for hypothesis in E7.HYPOTHESES:
            ordered = E7.rank_pairs(pairs, hypothesis)
            positions = {
                (pair["figure_id"], pair["ground_id"]): index
                for index, pair in enumerate(ordered, start=1)
            }
            view = HR.compare_reading(
                reading, positions, review_to_candidate, by_candidate, len(ordered)
            )
            top = ordered[0]
            view["model_top_pair"] = {
                "figure_label": top["semantic"]["figure_label"],
                "ground_label": top["semantic"]["ground_label"],
                "figure_hex": by_candidate[top["figure_id"]]["hex"],
                "ground_hex": by_candidate[top["ground_id"]]["hex"],
            }
            if view["stated_orientation"]:
                view["top_pair_distance"] = HR.top_pair_distance(
                    top, view["stated_orientation"], by_candidate
                )
            row["per_hypothesis"][hypothesis] = view
        result["readings"].append(row)
    return result


def evaluate(project_path: str) -> Path:
    """Re-evaluate E7's existing pair evidence against the recorded review."""
    project = Path(project_path)
    e6_root, e7_root = _paths(project)
    selections = _read_json(e6_root / "human-selections.json")
    out_dir = e7_root / CONSOLIDATION
    out_dir.mkdir(parents=True, exist_ok=True)

    images = [
        evaluate_one(entry["id"], e6_root, e7_root, entry)
        for entry in selections["records"]
        if entry.get("reviewed") and (e7_root / entry["id"] / "02-pair-evidence.json").exists()
    ]
    path = out_dir / "evaluation-human-review.json"
    _write_json(path, {
        "experiment": f"{E7_EXPERIMENT}/{CONSOLIDATION}",
        "created": datetime.now(timezone.utc).isoformat(),
        "method_note": (
            "E7 pair evidence and scoring were not regenerated. This re-ranks the "
            "persisted pair records and locates the recorded human readings in them."
        ),
        "status_note": (
            "The review is evidence about perception, not ground truth, and E7 "
            "was not tuned to it."
        ),
        "comparison_kinds": list(HR.COMPARISON_KINDS),
        "summary": {
            hypothesis: HR.summarise(images, hypothesis) for hypothesis in E7.HYPOTHESES
        },
        "images": images,
    })
    print(f"[consolidate] evaluated {len(images)} images -> {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-E7 consolidation")
    parser.add_argument("mode", choices=("record", "evaluate"))
    parser.add_argument("--project", required=True, help="Crossing project root")
    args = parser.parse_args()
    if args.mode == "record":
        record(args.project)
    else:
        evaluate(args.project)


if __name__ == "__main__":
    main()
