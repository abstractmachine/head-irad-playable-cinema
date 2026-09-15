"""Faithful machine-readable representation of the E6 human review.

The original E6 schema assumes one candidate per role. The reviewer did not
always work that way: some roles are a choice between alternatives, some are a
compound of several regions, and one image carries two whole competing
readings. Collapsing any of those to a single candidate would discard the
evidence, so this module represents all three states explicitly.

Nothing here scores or selects. It records what was perceived, resolves it
against the frozen E6 pool, and reports where the current representation cannot
express it.
"""

from __future__ import annotations

import math

# A role is one candidate, a choice between candidates, or several candidates
# acting together. These are not interchangeable.
ROLE_MODES = ("single", "alternative", "compound")

# E6 offered sufficient/insufficient only. The review needs a third state: the
# pool holds the right material but no single candidate expresses the role.
SUFFICIENCY = ("sufficient", "insufficient", "pieces_present_grouping_absent")

INSUFFICIENCY_REASONS = (
    "element_never_segmented",
    "relational_event_has_no_single_candidate",
    "measured_colour_contaminated_by_another_region",
    "small_accent_merged_away",
    "colour_only_present_as_unspatialized_reading",
    "two_colours_must_be_read_relationally",
    "no_candidate_expresses_the_visual_event",
    # Added by this consolidation; see review-protocol-extension.md.
    "perceptual_role_requires_compound_region",
)

ROLE_VOCABULARY = "figure_ground"
COMPARISON_KINDS = ("exact_single_pair", "alternative_pair", "representation_mismatch")


def validate_role(role: dict, known_review_ids: set[str], where: str) -> list[str]:
    """Return every problem with one role selection, rather than the first."""
    problems = []
    mode = role.get("mode")
    candidates = role.get("candidates") or []
    if mode not in ROLE_MODES:
        problems.append(f"{where}: mode {mode!r} is not one of {ROLE_MODES}")
    if not candidates:
        problems.append(f"{where}: no candidates recorded")
    for review_id in candidates:
        if review_id not in known_review_ids:
            problems.append(f"{where}: {review_id} is not in the frozen E6 pool")
    if mode == "single" and len(candidates) != 1:
        problems.append(f"{where}: mode 'single' needs exactly one candidate")
    if mode in ("alternative", "compound") and len(candidates) < 2:
        problems.append(f"{where}: mode {mode!r} needs at least two candidates")
    preferred = role.get("preferred")
    if preferred is not None and preferred not in candidates:
        problems.append(f"{where}: preferred {preferred!r} is not among its candidates")
    if mode == "compound" and preferred is not None:
        problems.append(f"{where}: a compound role cannot have a preferred member")
    return problems


def validate_record(record: dict, known_review_ids: set[str]) -> list[str]:
    """Check one image's review against the frozen pool it refers to."""
    problems = []
    image = record.get("id", "?")
    if record.get("role_vocabulary") != ROLE_VOCABULARY:
        problems.append(f"{image}: role_vocabulary must be {ROLE_VOCABULARY!r}")
    preferred = record.get("preferred") or {}
    for role_name in ("figure", "ground"):
        if role_name not in preferred:
            problems.append(f"{image}: preferred reading has no {role_name}")
            continue
        problems.extend(
            validate_role(preferred[role_name], known_review_ids,
                          f"{image}.preferred.{role_name}")
        )
    for index, alternative in enumerate(record.get("alternatives") or []):
        for role_name in ("figure", "ground"):
            if role_name not in alternative:
                problems.append(f"{image}: alternative {index} has no {role_name}")
                continue
            problems.extend(
                validate_role(alternative[role_name], known_review_ids,
                              f"{image}.alternatives[{index}].{role_name}")
            )
    pool = record.get("candidate_pool") or {}
    if pool.get("sufficiency") not in SUFFICIENCY:
        problems.append(f"{image}: sufficiency {pool.get('sufficiency')!r} is not in {SUFFICIENCY}")
    for reason in pool.get("reasons") or []:
        if reason not in INSUFFICIENCY_REASONS:
            problems.append(f"{image}: unknown pool reason {reason!r}")
    return problems


def readings(record: dict) -> list[dict]:
    """Return the preferred reading first, then any explicit alternatives."""
    out = [{"kind": "preferred", "index": 0, **(record.get("preferred") or {})}]
    for index, alternative in enumerate(record.get("alternatives") or [], start=1):
        out.append({"kind": "alternative", "index": index, **alternative})
    return out


def role_is_comparable(role: dict) -> bool:
    """A compound role has no single candidate a one-mask model could match."""
    return role.get("mode") in ("single", "alternative")


def reading_kind(reading: dict) -> str:
    figure, ground = reading["figure"], reading["ground"]
    if not (role_is_comparable(figure) and role_is_comparable(ground)):
        return "representation_mismatch"
    if figure["mode"] == "single" and ground["mode"] == "single":
        return "exact_single_pair"
    return "alternative_pair"


def resolve(role: dict, review_to_candidate: dict[str, str]) -> list[str]:
    """Map a role's review ids onto frozen candidate ids, order preserved."""
    return [
        review_to_candidate[review_id]
        for review_id in role.get("candidates") or []
        if review_id in review_to_candidate
    ]


def _delta_e(left_lab, right_lab) -> float:
    return round(math.dist([float(v) for v in left_lab], [float(v) for v in right_lab]), 3)


def compare_reading(reading: dict, positions: dict, review_to_candidate: dict,
                    by_candidate: dict, total: int) -> dict:
    """Locate one human reading inside an already-computed E7 ranking.

    Alternatives earn credit for any stated member. Orientation is measured
    separately from pair identity, because E7 found those can disagree.
    """
    kind = reading_kind(reading)
    result = {
        "kind": reading["kind"],
        "index": reading["index"],
        "comparison": kind,
        "figure_mode": reading["figure"].get("mode"),
        "ground_mode": reading["ground"].get("mode"),
        "figure_review_ids": list(reading["figure"].get("candidates") or []),
        "ground_review_ids": list(reading["ground"].get("candidates") or []),
        "rationale": reading.get("rationale", ""),
    }
    if kind == "representation_mismatch":
        result["reason"] = (
            "at least one role is a compound of several regions; E7 assigns one "
            "candidate per role and cannot express this reading"
        )
        result["stated_orientation"] = None
        result["reversed_orientation"] = None
        return result

    figure_ids = resolve(reading["figure"], review_to_candidate)
    ground_ids = resolve(reading["ground"], review_to_candidate)
    stated = _best(positions, figure_ids, ground_ids, by_candidate, total)
    reversed_ = _best(positions, ground_ids, figure_ids, by_candidate, total)
    result["stated_orientation"] = stated
    result["reversed_orientation"] = reversed_
    if stated and reversed_:
        result["orientation_preferred_by_model"] = (
            "stated" if stated["rank"] <= reversed_["rank"] else "reversed"
        )
        result["orientation_rank_gap"] = reversed_["rank"] - stated["rank"]
    return result


def _best(positions: dict, figure_ids: list[str], ground_ids: list[str],
          by_candidate: dict, total: int) -> dict | None:
    best = None
    for figure in figure_ids:
        for ground in ground_ids:
            rank = positions.get((figure, ground))
            if rank is None or (best is not None and rank >= best["rank"]):
                continue
            best = {
                "rank": rank,
                "of": total,
                "figure_candidate_id": figure,
                "ground_candidate_id": ground,
                "figure_label": by_candidate[figure]["label"],
                "ground_label": by_candidate[ground]["label"],
                "figure_hex": by_candidate[figure]["hex"],
                "ground_hex": by_candidate[ground]["hex"],
            }
    return best


def top_pair_distance(top_pair: dict, human: dict, by_candidate: dict) -> dict:
    """Measure how far the model's best pair is from a human reading."""
    top_figure = by_candidate[top_pair["figure_id"]]["lab"]
    top_ground = by_candidate[top_pair["ground_id"]]["lab"]
    human_figure = by_candidate[human["figure_candidate_id"]]["lab"]
    human_ground = by_candidate[human["ground_candidate_id"]]["lab"]
    same = max(_delta_e(top_figure, human_figure), _delta_e(top_ground, human_ground))
    swapped = max(_delta_e(top_figure, human_ground), _delta_e(top_ground, human_figure))
    return {
        "figure_delta_e": _delta_e(top_figure, human_figure),
        "ground_delta_e": _delta_e(top_ground, human_ground),
        "worst_delta_e_same_orientation": round(same, 3),
        "worst_delta_e_swapped_orientation": round(swapped, 3),
        "closer_when_swapped": swapped < same,
    }


def summarise(images: list[dict], hypothesis: str) -> dict:
    """Aggregate without turning the review into an accuracy score."""
    comparable, mismatched = [], []
    for image in images:
        rows = [
            reading for reading in image["readings"]
            if reading["comparison"] != "representation_mismatch"
            and reading["per_hypothesis"][hypothesis]["stated_orientation"]
        ]
        if rows:
            comparable.append((image, rows))
        if any(r["comparison"] == "representation_mismatch" for r in image["readings"]):
            mismatched.append(image["id"])

    best_ranks, relation_hits, orientation_hits = [], 0, 0
    stated_preferred, reversed_preferred = [], []
    for image, rows in comparable:
        ranked = min(
            rows, key=lambda row: row["per_hypothesis"][hypothesis]["stated_orientation"]["rank"]
        )
        view = ranked["per_hypothesis"][hypothesis]
        best_ranks.append(view["stated_orientation"]["rank"])
        # Orientation measured on the reviewer's own candidates, so that it is
        # not confounded with the model choosing a different instance of a role.
        if view.get("orientation_preferred_by_model") == "stated":
            stated_preferred.append(image["id"])
        elif view.get("orientation_preferred_by_model") == "reversed":
            reversed_preferred.append(image["id"])
        distance = view.get("top_pair_distance")
        if distance and min(
            distance["worst_delta_e_same_orientation"],
            distance["worst_delta_e_swapped_orientation"],
        ) <= RELATION_TOLERANCE_DELTA_E:
            relation_hits += 1
            if not distance["closer_when_swapped"]:
                orientation_hits += 1

    return {
        "images_compared": len(comparable),
        "images_with_representation_mismatch": sorted(set(mismatched)),
        "best_stated_rank_1": sum(1 for rank in best_ranks if rank == 1),
        "best_stated_rank_in_top_10": sum(1 for rank in best_ranks if rank <= 10),
        "best_stated_rank_in_top_50": sum(1 for rank in best_ranks if rank <= 50),
        "median_best_stated_rank": (
            int(sorted(best_ranks)[len(best_ranks) // 2]) if best_ranks else None
        ),
        "worst_best_stated_rank": max(best_ranks) if best_ranks else None,
        "top_pair_recovers_the_relation": relation_hits,
        "top_pair_also_orients_it_as_stated": orientation_hits,
        "stated_orientation_preferred": stated_preferred,
        "reversed_orientation_preferred": reversed_preferred,
        "orientation_note": (
            "Measured by ranking the reviewer's own two candidates in both "
            "directions, which separates orientation from the model's freedom to "
            "pick a different instance of the same role."
        ),
        "relation_tolerance_delta_e": RELATION_TOLERANCE_DELTA_E,
        "tolerance_note": (
            "A reporting tolerance for 'the same two colours', not a scoring "
            "threshold. Raw delta E is recorded per image."
        ),
    }


# Several E4 lenses measure one region to within a couple of delta E, so exact
# candidate identity understates agreement. Reporting only; nothing is scored.
RELATION_TOLERANCE_DELTA_E = 3.0
