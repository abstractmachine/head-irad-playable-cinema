"""E6 — human-reviewed two-colour pair selection over the frozen E5 pool.

E6 does not modify E5.  It rebuilds the same measured candidate pool so a human
reviewer has pixels to look at, verifies that rebuild against the frozen E5
inventory, and records what a human actually selects as ``focus`` and
``ambiance``.

The reviewer's answers are *evidence about human intent*, not ground truth and
not a target to optimise against.  Nothing in this module scores candidates,
reranks pairs, or feeds a human selection back into E5.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import numpy as np


# Diagnostic images are reviewed first; see review-protocol.md section 3.
DIAGNOSTIC_ORDER = ["PAL-001", "PAL-004", "PAL-015", "PAL-022", "PAL-019"]
REMAINING_ORDER = ["PAL-005", "PAL-017", "PAL-020", "PAL-030"]

PAIR_ASSESSMENTS = ("strong", "adequate", "weak")
POOL_SUFFICIENCY = ("sufficient", "insufficient")

# Suggested, deliberately non-exhaustive vocabulary. Reviewers may add reasons.
INSUFFICIENCY_REASONS = (
    "element_never_segmented",
    "relational_event_has_no_single_candidate",
    "measured_colour_contaminated_by_another_region",
    "small_accent_merged_away",
    "colour_only_present_as_unspatialized_reading",
    "two_colours_must_be_read_relationally",
    "no_candidate_expresses_the_visual_event",
)

CELL_NAMES = (
    ("upper_left", "upper_center", "upper_right"),
    ("left", "center", "right"),
    ("lower_left", "lower_center", "lower_right"),
)


def review_order() -> list[str]:
    """Return the pilot in review order: five diagnostics, then the rest."""
    return list(DIAGNOSTIC_ORDER) + list(REMAINING_ORDER)


def observed_cell(centroid) -> str:
    """Name the 3x3 cell a measured centroid falls in, using E4's vocabulary."""
    if not centroid or any(value is None for value in centroid):
        return ""
    column = min(2, max(0, int(float(centroid[0]) * 3)))
    row = min(2, max(0, int(float(centroid[1]) * 3)))
    return CELL_NAMES[row][column]


def delta_e(left_lab, right_lab) -> float:
    """Plain CIE76 distance, reported as a raw measurement with no threshold."""
    return round(math.dist([float(v) for v in left_lab], [float(v) for v in right_lab]), 3)


def mask_iou(left: np.ndarray, right: np.ndarray) -> float:
    shared = int(np.count_nonzero(left & right))
    union = int(np.count_nonzero(left | right))
    return round(shared / union, 4) if union else 0.0


def _candidate_label(candidate: dict) -> str:
    if candidate["source"] == "semantic":
        return str(candidate["semantic"]["segment_phrase"])
    hierarchy = candidate["hierarchy"]
    return f"level {hierarchy['level']} region {hierarchy['region_identity']}"


def review_record(candidate: dict, review_id: str) -> dict:
    """Shape one measured candidate into reviewer-facing facts only.

    Every field here is something the reviewer can see or verify in the frame.
    No E5 score, rank, or selection is carried across.
    """
    provenance = candidate.get("provenance", {})
    record = {
        "review_id": review_id,
        "candidate_id": candidate["candidate_id"],
        "source": candidate["source"],
        "label": _candidate_label(candidate),
        "hex": candidate["hex"],
        "rgb": [int(value) for value in candidate["rgb"]],
        "lab": candidate["lab"],
        "area_pixels": int(candidate["area"]),
        "area_percent": round(float(candidate["coverage"]) * 100.0, 3),
        "centroid": candidate["centroid"],
        "observed_cell": observed_cell(candidate["centroid"]),
        "bbox": candidate["bbox"],
        "semantic_role": None,
        "interpretation": "",
        "declared_location": "",
        "declared_extent": "",
        "hierarchy_level": None,
        "hierarchy_region": None,
    }
    if candidate["source"] == "semantic":
        record["semantic_role"] = candidate["semantic"]["role"]
        record["interpretation"] = provenance.get("interpretation", "")
        record["declared_location"] = provenance.get("declared_location", "")
        record["declared_extent"] = provenance.get("declared_extent", "")
    else:
        record["hierarchy_level"] = candidate["hierarchy"]["level"]
        record["hierarchy_region"] = candidate["hierarchy"]["region_identity"]
    return record


def build_review_records(candidates: list[dict]) -> list[dict]:
    """Number the pool in frozen inventory order, which is not score order."""
    return [
        review_record(candidate, f"C{index:02d}")
        for index, candidate in enumerate(candidates, start=1)
    ]


def verify_against_frozen_e5(candidates: list[dict], frozen_inventory: dict) -> dict:
    """Check that the E6 rebuild reproduced the frozen E5 candidate pool.

    Drift is reported as evidence rather than corrected: if SAM3 replay differs
    from the E5 run, the reviewer is looking at slightly different pixels and
    the comparison must say so.
    """
    rebuilt = {candidate["candidate_id"]: candidate for candidate in candidates}
    frozen = {
        candidate["candidate_id"]: candidate
        for candidate in frozen_inventory.get("measured_candidates", [])
    }
    drift = []
    for candidate_id in sorted(set(rebuilt) & set(frozen)):
        mine, theirs = rebuilt[candidate_id], frozen[candidate_id]
        if list(mine["rgb"]) == list(theirs["rgb"]) and int(mine["area"]) == int(theirs["area"]):
            continue
        drift.append({
            "candidate_id": candidate_id,
            "e5": {"rgb": theirs["rgb"], "hex": theirs["hex"], "area": theirs["area"]},
            "e6": {"rgb": mine["rgb"], "hex": mine["hex"], "area": mine["area"]},
            "delta_e": delta_e(mine["lab"], theirs["lab"]),
        })
    return {
        "frozen_candidates": len(frozen),
        "rebuilt_candidates": len(rebuilt),
        "matched": len(set(rebuilt) & set(frozen)),
        "missing_from_rebuild": sorted(set(frozen) - set(rebuilt)),
        "absent_from_e5": sorted(set(rebuilt) - set(frozen)),
        "measurement_drift": drift,
        "identical_pool": (
            set(rebuilt) == set(frozen) and not drift
        ),
    }


def selection_template(pal: str, records: list[dict], phase: str) -> dict:
    """Emit one blank, machine-readable review record for a single image."""
    return {
        "id": pal,
        "phase": phase,
        "reviewer": "",
        "reviewed": False,
        "focus": {"review_id": "", "candidate_id": ""},
        "ambiance": {"review_id": "", "candidate_id": ""},
        "pair_assessment": "",
        "rationale": "",
        "candidate_pool": {
            "sufficiency": "",
            "reasons": [],
            "notes": "",
        },
        "notes": "",
        "_choices": {
            "review_ids": [record["review_id"] for record in records],
            "pair_assessment": list(PAIR_ASSESSMENTS),
            "sufficiency": list(POOL_SUFFICIENCY),
            "suggested_reasons": list(INSUFFICIENCY_REASONS),
        },
    }


# ---------------------------------------------------------------------------
# Comparison — human selection against the frozen E5 selection
# ---------------------------------------------------------------------------

def _resolve(selection: dict, by_review_id: dict, by_candidate_id: dict) -> dict | None:
    review_id = str(selection.get("review_id") or "").strip().upper()
    candidate_id = str(selection.get("candidate_id") or "").strip()
    if review_id and review_id in by_review_id:
        return by_review_id[review_id]
    if candidate_id and candidate_id in by_candidate_id:
        return by_candidate_id[candidate_id]
    return None


def _measurement(human: dict, e5: dict, masks: dict[str, np.ndarray] | None) -> dict:
    measurement = {
        "human_hex": human["hex"],
        "e5_hex": e5["hex"],
        "same_exact_rgb": list(human["rgb"]) == list(e5["rgb"]),
        "delta_e": delta_e(human["lab"], e5["lab"]),
        "same_source": human["source"] == e5["source"],
        "human_area_percent": human["area_percent"],
        "e5_area_percent": e5["area_percent"],
        "mask_iou": None,
    }
    if masks and human["candidate_id"] in masks and e5["candidate_id"] in masks:
        measurement["mask_iou"] = mask_iou(
            masks[human["candidate_id"]], masks[e5["candidate_id"]]
        )
    return measurement


def _pair_rank(ranking: dict, focus_id: str, ambiance_id: str) -> dict:
    """Locate an ordered pair inside the frozen E5 ranking, read-only."""
    pairs = ranking.get("ranked_ordered_pairs") or []
    for position, pair in enumerate(pairs, start=1):
        if pair.get("focus_id") == focus_id and pair.get("ambiance_id") == ambiance_id:
            return {
                "rank": position,
                "of": len(pairs),
                "percentile": round(1.0 - (position - 1) / max(1, len(pairs)), 4),
                "pair_score": pair.get("score"),
            }
    return {"rank": None, "of": len(pairs), "percentile": None, "pair_score": None}


def _role_rank(ranking: dict, candidate_id: str, role: str) -> dict:
    rows = ranking.get("candidate_ranking") or []
    ordered = sorted(rows, key=lambda row: -float(row[role]["score"]))
    for position, row in enumerate(ordered, start=1):
        if row["candidate_id"] == candidate_id:
            return {"rank": position, "of": len(ordered), "score": row[role]["score"]}
    return {"rank": None, "of": len(ordered), "score": None}


def classify(selection: dict, focus_exact: bool, ambiance_exact: bool) -> str:
    """Keep 'E5 mis-selected' and 'the pool cannot express it' separate."""
    sufficiency = str(selection.get("candidate_pool", {}).get("sufficiency") or "").lower()
    if sufficiency == "insufficient":
        return "pool_insufficient"
    if focus_exact and ambiance_exact:
        return "exact_pair_agreement"
    if focus_exact or ambiance_exact:
        return "partial_agreement_present_but_unselected"
    return "present_but_unselected"


def compare_one(selection: dict, records: list[dict], e5_reduction: dict,
                e5_ranking: dict, masks: dict[str, np.ndarray] | None = None) -> dict:
    """Compare one reviewer's answer for one image against frozen E5."""
    by_review_id = {record["review_id"]: record for record in records}
    by_candidate_id = {record["candidate_id"]: record for record in records}
    e5_ids = {
        role: e5_reduction["roles"][role]["candidate_id"]
        for role in ("focus", "ambiance")
    }
    e5_records = {
        role: by_candidate_id.get(candidate_id) for role, candidate_id in e5_ids.items()
    }
    human_records = {
        role: _resolve(selection.get(role) or {}, by_review_id, by_candidate_id)
        for role in ("focus", "ambiance")
    }
    unresolved = [role for role, record in human_records.items() if record is None]

    comparison = {
        "id": selection.get("id"),
        "reviewer": selection.get("reviewer", ""),
        "phase": selection.get("phase", ""),
        "unresolved_roles": unresolved,
        "human": {
            role: (
                {key: record[key] for key in
                 ("review_id", "candidate_id", "source", "label", "hex", "rgb",
                  "semantic_role", "hierarchy_level", "area_percent", "observed_cell")}
                if record else None
            )
            for role, record in human_records.items()
        },
        "e5": {
            role: (
                {key: record[key] for key in
                 ("review_id", "candidate_id", "source", "label", "hex", "rgb",
                  "semantic_role", "hierarchy_level", "area_percent", "observed_cell")}
                if record else {"candidate_id": e5_ids[role]}
            )
            for role, record in e5_records.items()
        },
        "pair_assessment": selection.get("pair_assessment", ""),
        "rationale": selection.get("rationale", ""),
        "candidate_pool": selection.get("candidate_pool", {}),
        "notes": selection.get("notes", ""),
        "equivalence_rule": None,
        "equivalence_note": (
            "Raw delta E and mask IoU are reported. No 'essentially the same "
            "colour' rule is applied until the measurements have been inspected."
        ),
    }
    if unresolved:
        comparison["agreement"] = None
        comparison["classification"] = "unresolved_selection"
        return comparison

    focus_exact = human_records["focus"]["candidate_id"] == e5_ids["focus"]
    ambiance_exact = human_records["ambiance"]["candidate_id"] == e5_ids["ambiance"]
    comparison["agreement"] = {
        "focus_exact": focus_exact,
        "ambiance_exact": ambiance_exact,
        "pair_exact": focus_exact and ambiance_exact,
        "role_swap": (
            human_records["focus"]["candidate_id"] == e5_ids["ambiance"]
            and human_records["ambiance"]["candidate_id"] == e5_ids["focus"]
        ),
        "source_pattern": {
            "human": f"{human_records['focus']['source']}/{human_records['ambiance']['source']}",
            "e5": "/".join(
                (e5_records[role] or {}).get("source", "unknown")
                for role in ("focus", "ambiance")
            ),
        },
        "focus_measurement": (
            _measurement(human_records["focus"], e5_records["focus"], masks)
            if e5_records["focus"] else None
        ),
        "ambiance_measurement": (
            _measurement(human_records["ambiance"], e5_records["ambiance"], masks)
            if e5_records["ambiance"] else None
        ),
    }
    comparison["e5_view_of_human_choice"] = {
        "pair": _pair_rank(
            e5_ranking,
            human_records["focus"]["candidate_id"],
            human_records["ambiance"]["candidate_id"],
        ),
        "selected_pair": _pair_rank(e5_ranking, e5_ids["focus"], e5_ids["ambiance"]),
        "human_focus_by_focus_score": _role_rank(
            e5_ranking, human_records["focus"]["candidate_id"], "focus"
        ),
        "human_ambiance_by_ambiance_score": _role_rank(
            e5_ranking, human_records["ambiance"]["candidate_id"], "ambiance"
        ),
    }
    comparison["classification"] = classify(selection, focus_exact, ambiance_exact)
    return comparison


def census(records: list[dict], ranking: dict, reduction: dict) -> list[dict]:
    """Locate every measured candidate inside the frozen E5 ranking.

    This reads E5 and changes nothing. It exists so that "the accent survived
    in the candidate evidence but did not win" can be stated as a position
    rather than an impression. It must not be shown to a reviewer beforehand.
    """
    selected = {
        role: reduction["roles"][role]["candidate_id"] for role in ("focus", "ambiance")
    }
    best_as: dict[str, dict[str, int]] = {}
    for position, pair in enumerate(ranking.get("ranked_ordered_pairs") or [], start=1):
        for role, key in (("focus", "focus_id"), ("ambiance", "ambiance_id")):
            entry = best_as.setdefault(pair[key], {})
            entry.setdefault(role, position)

    rows = []
    for record in records:
        candidate_id = record["candidate_id"]
        rows.append({
            "review_id": record["review_id"],
            "candidate_id": candidate_id,
            "source": record["source"],
            "label": record["label"],
            "semantic_role": record["semantic_role"],
            "hex": record["hex"],
            "area_percent": record["area_percent"],
            "e5_focus": _role_rank(ranking, candidate_id, "focus"),
            "e5_ambiance": _role_rank(ranking, candidate_id, "ambiance"),
            "best_pair_rank_as_focus": best_as.get(candidate_id, {}).get("focus"),
            "best_pair_rank_as_ambiance": best_as.get(candidate_id, {}).get("ambiance"),
            "e5_selected_as": next(
                (role for role, chosen in selected.items() if chosen == candidate_id), None
            ),
        })
    return rows


def summarise(comparisons: list[dict]) -> dict:
    """Aggregate without collapsing reviewers into a single assumed answer."""
    reviewers = sorted({row.get("reviewer", "") for row in comparisons if row.get("reviewer")})
    scored = [row for row in comparisons if row.get("agreement")]
    per_reviewer = {}
    for reviewer in reviewers:
        rows = [row for row in scored if row.get("reviewer") == reviewer]
        per_reviewer[reviewer] = {
            "reviewed_images": len(rows),
            "focus_exact": sum(1 for row in rows if row["agreement"]["focus_exact"]),
            "ambiance_exact": sum(1 for row in rows if row["agreement"]["ambiance_exact"]),
            "pair_exact": sum(1 for row in rows if row["agreement"]["pair_exact"]),
            "role_swap": sum(1 for row in rows if row["agreement"]["role_swap"]),
            "classifications": dict(Counter(row["classification"] for row in rows)),
            "pair_assessments": dict(Counter(
                row.get("pair_assessment", "") for row in rows
            )),
            "human_source_patterns": dict(Counter(
                row["agreement"]["source_pattern"]["human"] for row in rows
            )),
        }

    disagreement = {}
    for pal in sorted({row["id"] for row in scored if row.get("id")}):
        rows = [row for row in scored if row["id"] == pal]
        focus_choices = {row["reviewer"]: row["human"]["focus"]["candidate_id"] for row in rows}
        ambiance_choices = {row["reviewer"]: row["human"]["ambiance"]["candidate_id"] for row in rows}
        disagreement[pal] = {
            "reviewers": sorted(focus_choices),
            "focus_choices": focus_choices,
            "ambiance_choices": ambiance_choices,
            "focus_unanimous": len(set(focus_choices.values())) <= 1,
            "ambiance_unanimous": len(set(ambiance_choices.values())) <= 1,
        }

    return {
        "reviewer_count": len(reviewers),
        "single_reviewer": len(reviewers) == 1,
        "single_reviewer_note": (
            "Exactly one reviewer contributed. This is recorded explicitly; a "
            "single reviewer's answer is not treated as ground truth."
            if len(reviewers) == 1 else ""
        ),
        "reviewers": reviewers,
        "records_supplied": len(comparisons),
        "records_compared": len(scored),
        "per_reviewer": per_reviewer,
        "reviewer_disagreement": disagreement,
        "pool_insufficient_images": sorted({
            row["id"] for row in scored if row["classification"] == "pool_insufficient"
        }),
        "present_but_unselected_images": sorted({
            row["id"] for row in scored
            if row["classification"] in
            ("present_but_unselected", "partial_agreement_present_but_unselected")
        }),
    }


def load_masks(path: Path) -> dict[str, np.ndarray]:
    """Load the boolean candidate masks persisted at build time."""
    with np.load(path, allow_pickle=False) as archive:
        return {key: archive[key].astype(bool) for key in archive.files}
