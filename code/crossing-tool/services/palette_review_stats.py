"""Census, provenance, distance, failure and sequence analysis.

Split from ``palette_review_analysis`` so the normalized-record extraction stays
readable. Everything here is a pure function over the normalized records.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from data import palette_review as store
from services.palette_review_analysis import (
    COHORT_FULL, SOURCE_MANUAL, SOURCE_PROPOSAL, distance,
)

ROLES = store.ROLES
FIGURE, BACKGROUND = store.ROLE_FIGURE, store.ROLE_BACKGROUND

# Segmentation statuses that are positive evidence the mask was not what the
# material named. Anything else is treated as "the measurement was plausible".
BAD_SEGMENTATION = ("no_mask", "error", "unusable", "suspicious_broad")
SUSPECT_SEGMENTATION = BAD_SEGMENTATION + ("multiple_masks",)

# Failure classes, kept distinct per the analysis brief.
FAILURE_INTERPRETATION = "A_interpretation"
FAILURE_MATERIAL = "B_material"
FAILURE_MEASUREMENT = "C_segmentation_measurement"
FAILURE_REDUCTION = "D_reduction"
FAILURE_NONE = "no_failure_close_match"
FAILURE_UNCERTAIN = "uncertain"


def _origin_key(record: dict | None) -> str:
    if not record:
        return "none"
    if record.get("source") == SOURCE_MANUAL:
        return "manual"
    choice = record.get("choice")
    return f"P{choice}" if choice else "proposal_unknown"


# ---------------------------------------------------------------------------
# 3. Census
# ---------------------------------------------------------------------------

def census(rows: list) -> dict:
    states = Counter(row["state"] for row in rows)
    cohorts = Counter(row["cohort"] for row in rows)

    shapes = Counter()
    for row in rows:
        roles = row["roles"]
        sources = {role: (roles.get(role) or {}).get("source") for role in ROLES}
        manual = [role for role, src in sources.items() if src == SOURCE_MANUAL]
        present = [role for role, src in sources.items() if src]
        if len(present) < 2:
            shapes["incomplete"] += 1
        elif not manual:
            choices = {(roles[role] or {}).get("choice") for role in ROLES}
            shapes["whole_proposal" if len(choices) == 1 else "split_two_proposals"] += 1
        elif len(manual) == 2:
            shapes["fully_manual"] += 1
        else:
            shapes[f"manual_{manual[0]}_only"] += 1

    strategies = {role: Counter() for role in ROLES}
    origins = {role: Counter() for role in ROLES}
    for row in rows:
        for role in ROLES:
            record = row["roles"].get(role)
            origins[role][_origin_key(record)] += 1
            if record and record.get("source") == SOURCE_PROPOSAL:
                strategies[role][record.get("strategy") or "unknown"] += 1
            elif record:
                strategies[role]["manual"] += 1

    total = len(rows)
    return {
        "total_frames": total,
        "states": dict(states),
        "cohorts": dict(cohorts),
        "decision_shapes": dict(shapes),
        "role_origin_counts": {role: dict(origins[role]) for role in ROLES},
        "role_origin_percent": {
            role: {key: round(100.0 * value / total, 1)
                   for key, value in origins[role].most_common()}
            for role in ROLES
        },
        "role_strategy_counts": {role: dict(strategies[role]) for role in ROLES},
        "role_strategy_percent": {
            role: {key: round(100.0 * value / total, 1)
                   for key, value in strategies[role].most_common()}
            for role in ROLES
        },
        "generation": {
            "ok": sum(1 for r in rows if r["generation_status"] == "ok"),
            "missing": sum(1 for r in rows if r["generation_status"] != "ok"),
        },
    }


def split_matrix(rows: list) -> dict:
    """figure origin x background origin, over every reviewed frame."""
    keys = ["P1", "P2", "P3", "P4", "manual", "none"]
    matrix = {fig: {bg: 0 for bg in keys} for fig in keys}
    for row in rows:
        fig = _origin_key(row["roles"].get(FIGURE))
        bg = _origin_key(row["roles"].get(BACKGROUND))
        if fig in matrix and bg in matrix[fig]:
            matrix[fig][bg] += 1
    used = [k for k in keys if any(matrix[k].values()) or
            any(matrix[f][k] for f in keys)]
    total = len(rows)
    same = sum(matrix[k][k] for k in keys if k.startswith("P"))
    return {
        "axes": {"rows": "figure", "columns": "background"},
        "keys": used,
        "counts": {fig: {bg: matrix[fig][bg] for bg in used} for fig in used},
        "both_from_same_proposal": same,
        "both_from_same_proposal_percent": round(100.0 * same / total, 1),
        "mixed_sources": total - same,
    }


# ---------------------------------------------------------------------------
# 4. Colour distance
# ---------------------------------------------------------------------------

def _proposal_colours(row: dict, role: str) -> list:
    """Every generated colour offered for *role*, with its provenance."""
    out = []
    for key, proposal in sorted(row["proposals"].items()):
        entry = proposal["roles"].get(role) or {}
        colour = entry.get("colour")
        if colour and colour.get("lab"):
            out.append({
                "choice": key,
                "strategy": proposal["strategy"],
                "active": proposal["active"],
                "lab": colour["lab"],
                "hex": colour.get("hex"),
                "material": entry.get("material"),
                "segmentation_status": entry.get("segmentation_status"),
                "coverage": entry.get("coverage"),
            })
    return out


def manual_distances(rows: list) -> list:
    """For every hand-picked role, its distance to all generated alternatives.

    Computed for every frame regardless of cohort, because it depends only on
    the frozen proposals and the final colour — never on the ``supersedes``
    link, which exists for only part of this review.
    """
    records = []
    for row in rows:
        for role in ROLES:
            picked = row["roles"].get(role)
            if not picked or picked.get("source") != SOURCE_MANUAL:
                continue
            lab = picked.get("lab")
            if not lab:
                continue
            same = _proposal_colours(row, role)
            other = _proposal_colours(row, BACKGROUND if role == FIGURE else FIGURE)
            for item in same:
                item["delta_e"] = distance(lab, item["lab"])
            for item in other:
                item["delta_e"] = distance(lab, item["lab"])
            ranked = sorted([i for i in same if i["delta_e"] is not None],
                            key=lambda i: i["delta_e"])
            cross = sorted([i for i in other if i["delta_e"] is not None],
                           key=lambda i: i["delta_e"])
            nearest = ranked[0] if ranked else None
            nearest_cross = cross[0] if cross else None
            records.append({
                "shot_id": row["shot_id"],
                "index": row["index"],
                "scene": row["scene"],
                "cohort": row["cohort"],
                "role": role,
                "hex": picked.get("hex"),
                "lab": lab,
                "supersedes": picked.get("supersedes"),
                "same_role": ranked,
                "nearest": nearest,
                "nearest_cross_role": nearest_cross,
                "role_reversal": bool(
                    nearest and nearest_cross
                    and nearest_cross["delta_e"] < nearest["delta_e"] - 5.0
                ),
                "active_alternatives": sum(1 for i in ranked if i["active"]),
            })
    return records


def _buckets(values: list, edges=(2, 5, 10, 20, 40)) -> dict:
    out = Counter()
    for value in values:
        placed = False
        low = 0
        for edge in edges:
            if value < edge:
                out[f"{low}-{edge}"] += 1
                placed = True
                break
            low = edge
        if not placed:
            out[f"{edges[-1]}+"] += 1
    return dict(out)


def _summary(values: list) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    def pct(p):
        return round(ordered[min(len(ordered) - 1, int(p * len(ordered)))], 2)
    return {
        "n": len(ordered),
        "min": round(ordered[0], 2),
        "p10": pct(0.10), "p25": pct(0.25), "median": pct(0.50),
        "p75": pct(0.75), "p90": pct(0.90),
        "max": round(ordered[-1], 2),
        "mean": round(sum(ordered) / len(ordered), 2),
    }


def distance_analysis(records: list) -> dict:
    per_role = {}
    for role in ROLES:
        subset = [r for r in records if r["role"] == role and r["nearest"]]
        nearest = [r["nearest"]["delta_e"] for r in subset]
        nearest_active = [
            min((i["delta_e"] for i in r["same_role"] if i["active"]), default=None)
            for r in subset
        ]
        nearest_active = [v for v in nearest_active if v is not None]
        by_strategy = Counter(r["nearest"]["strategy"] for r in subset)
        seg = Counter(r["nearest"].get("segmentation_status") for r in subset)
        per_role[role] = {
            "manual_picks": len([r for r in records if r["role"] == role]),
            "with_any_generated_alternative": len(subset),
            "delta_e_to_nearest_generated": _summary(nearest),
            "delta_e_buckets": _buckets(nearest),
            "delta_e_to_nearest_ACTIVE_generated": _summary(nearest_active),
            "nearest_is_strategy": dict(by_strategy),
            "nearest_segmentation_status": {str(k): v for k, v in seg.items()},
            "role_reversals": sum(1 for r in subset if r["role_reversal"]),
        }
    return {
        "metric": "CIE76 delta-E (scripts.palette_lab.relational.delta_e)",
        "note": (
            "Distances are to the frozen measured proposal colours. They are "
            "independent of the supersedes link, which only exists for part of "
            "this review."
        ),
        "per_role": per_role,
    }


# ---------------------------------------------------------------------------
# 8. Failure classification
# ---------------------------------------------------------------------------

def classify_failure(record: dict, row: dict) -> dict:
    """Provisional failure class for one manual pick, with its evidence.

    Deliberately conservative: anything the record cannot support becomes
    ``uncertain`` rather than being forced into a class.
    """
    nearest = record.get("nearest")
    role = record["role"]
    proposals = row["proposals"]
    active = [p for p in proposals.values() if p["active"]]
    evidence = []

    if not active:
        return {"class": FAILURE_REDUCTION, "confidence": "low",
                "evidence": ["no active proposal was offered for this frame"]}

    if nearest is None:
        return {"class": FAILURE_UNCERTAIN, "confidence": "low",
                "evidence": ["no generated colour for this role to compare with"]}

    delta = nearest["delta_e"]
    statuses = [
        (p["roles"][role] or {}).get("segmentation_status")
        for p in proposals.values() if p["active"]
    ]
    bad = [s for s in statuses if s in SUSPECT_SEGMENTATION]

    if delta <= 3.0:
        evidence.append(f"nearest generated colour is dE {delta} away")
        return {"class": FAILURE_NONE, "confidence": "high", "evidence": evidence}

    if record["role_reversal"]:
        cross = record["nearest_cross_role"]
        evidence.append(
            f"closer to choice {cross['choice']}'s other role "
            f"(dE {cross['delta_e']}) than to any colour offered for {role} "
            f"(dE {delta})")
        return {"class": FAILURE_INTERPRETATION, "confidence": "medium",
                "evidence": evidence}

    if bad and delta > 10.0:
        evidence.append(
            f"segmentation for this role was {sorted(set(bad))} and the pick is "
            f"dE {delta} from the nearest generated colour")
        return {"class": FAILURE_MEASUREMENT, "confidence": "medium",
                "evidence": evidence}

    if bad:
        evidence.append(
            f"segmentation for this role was {sorted(set(bad))}; pick is dE {delta} away")
        return {"class": FAILURE_MEASUREMENT, "confidence": "low",
                "evidence": evidence}

    if delta > 25.0:
        evidence.append(
            f"dE {delta} from every generated colour for this role, all of which "
            f"segmented cleanly")
        return {"class": FAILURE_INTERPRETATION, "confidence": "low",
                "evidence": evidence}

    evidence.append(
        f"dE {delta} from the nearest generated colour ({nearest['strategy']}), "
        f"segmentation reported {nearest.get('segmentation_status')}")
    return {"class": FAILURE_MATERIAL, "confidence": "low", "evidence": evidence}


def failure_analysis(records: list, rows: list) -> dict:
    by_shot = {row["shot_id"]: row for row in rows}
    classified = []
    for record in records:
        row = by_shot.get(record["shot_id"])
        if not row:
            continue
        verdict = classify_failure(record, row)
        classified.append({**record, "failure": verdict})

    counts = {role: Counter() for role in ROLES}
    confidence = Counter()
    for item in classified:
        counts[item["role"]][item["failure"]["class"]] += 1
        confidence[item["failure"]["confidence"]] += 1
    return {
        "caveat": (
            "These are provisional classes derived from measurable evidence "
            "(delta-E, segmentation status, role reversal). They are a way to "
            "prioritise visual inspection, not a verdict. Confidence is stated "
            "per record and most are low."
        ),
        "per_role": {role: dict(counts[role]) for role in ROLES},
        "confidence": dict(confidence),
        "records": classified,
    }


# ---------------------------------------------------------------------------
# 9. Sequence
# ---------------------------------------------------------------------------

def sequence_analysis(rows: list) -> dict:
    ordered = [r for r in rows if r["index"] is not None]
    ordered.sort(key=lambda r: r["index"])

    steps = {role: {"same_scene": [], "scene_change": []} for role in ROLES}
    origin_runs = {role: [] for role in ROLES}
    for role in ROLES:
        run_key, run_len = None, 0
        for previous, current in zip(ordered, ordered[1:]):
            left = (previous["roles"].get(role) or {}).get("lab")
            right = (current["roles"].get(role) or {}).get("lab")
            value = distance(left, right)
            if value is not None:
                same = previous["scene"] == current["scene"]
                steps[role]["same_scene" if same else "scene_change"].append(value)
        for row in ordered:
            key = _origin_key(row["roles"].get(role))
            if key == run_key:
                run_len += 1
            else:
                if run_key is not None:
                    origin_runs[role].append({"origin": run_key, "length": run_len})
                run_key, run_len = key, 1
        if run_key is not None:
            origin_runs[role].append({"origin": run_key, "length": run_len})

    scenes = defaultdict(list)
    for row in ordered:
        if row["scene"]:
            scenes[row["scene"]].append(row)
    within = {role: [] for role in ROLES}
    for members in scenes.values():
        if len(members) < 2:
            continue
        for role in ROLES:
            labs = [(m["roles"].get(role) or {}).get("lab") for m in members]
            labs = [item for item in labs if item]
            for i in range(len(labs)):
                for j in range(i + 1, len(labs)):
                    value = distance(labs[i], labs[j])
                    if value is not None:
                        within[role].append(value)

    return {
        "ordered_frames": len(ordered),
        "scenes": len(scenes),
        "consecutive_step": {
            role: {
                "same_scene": _summary(steps[role]["same_scene"]),
                "scene_change": _summary(steps[role]["scene_change"]),
            }
            for role in ROLES
        },
        "within_scene_pairwise": {role: _summary(within[role]) for role in ROLES},
        "longest_origin_runs": {
            role: sorted(origin_runs[role], key=lambda r: -r["length"])[:12]
            for role in ROLES
        },
        "origin_run_length": {
            role: _summary([float(r["length"]) for r in origin_runs[role]])
            for role in ROLES
        },
    }
