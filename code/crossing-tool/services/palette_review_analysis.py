"""Read-only analysis of a completed human palette review.

    crossing index palette review-analysis --film "<name>"

Answers "what did the reviewer actually do?" over a whole film. It reads the
frozen review record and the frozen proposals and writes only beneath
``outputs/tests/palette-review-analysis/<media_id>/``. It never mutates the
review, never regenerates proposals, and never re-measures a mask.

Figure and background are treated as two independent decisions throughout.
"What proposal won?" is a derived view, not the unit of analysis.

The colour metric is the lab's existing CIE76 ``delta_e``; nothing new is
invented here. Manual picks store only RGB, so their Lab is computed with the
production ``data.palette._rgb_to_lab`` rather than a second conversion.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from data import palette_review as store

ANALYSIS_VERSION = "review-analysis-v1"
OUTPUT_SPACE = "palette-review-analysis"

# The review of tmdb_95864 straddled two feature additions to the store. Frames
# are tagged with the cohort they were last touched in so an analysis can say
# which evidence is admissible for them rather than silently averaging over it.
COHORT_PRE_SPLIT = "pre_split"
COHORT_PRE_SUPERSEDES = "pre_supersedes"
COHORT_FULL = "full"

SOURCE_MANUAL = store.SOURCE_MANUAL
SOURCE_PROPOSAL = store.SOURCE_PROPOSAL


# ---------------------------------------------------------------------------
# Colour helpers — the project's existing machinery only
# ---------------------------------------------------------------------------

def to_lab(rgb) -> list[float] | None:
    """Lab for an RGB triplet, via the production converter."""
    if not rgb:
        return None
    import numpy as np

    from data.palette import _rgb_to_lab

    values = np.asarray([[int(v) for v in rgb]], dtype=np.uint8)
    return [round(float(v), 2) for v in _rgb_to_lab(values)[0]]


def distance(left_lab, right_lab) -> float | None:
    """CIE76 delta-E, the lab's existing metric."""
    from scripts.palette_lab.relational import delta_e

    if not left_lab or not right_lab:
        return None
    return delta_e(left_lab, right_lab)


def _colour_block(colour: dict | None) -> dict | None:
    if not isinstance(colour, dict):
        return None
    rgb = colour.get("rgb")
    if not rgb:
        return None
    return {
        "rgb": [int(v) for v in rgb],
        "hex": colour.get("hex"),
        "lab": colour.get("lab") or to_lab(rgb),
    }


# ---------------------------------------------------------------------------
# Cohorts
# ---------------------------------------------------------------------------

def _stamp(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def frame_touched(entry: dict) -> datetime | None:
    """When the reviewer last decided anything about this frame."""
    times = [_stamp((entry.get("review") or {}).get("timestamp"))]
    times += [_stamp(rec.get("timestamp"))
              for rec in (entry.get("manual") or {}).values()]
    times = [item for item in times if item is not None]
    return max(times) if times else None


def feature_boundaries(frames: dict) -> dict:
    """When split validation and the supersedes link first appear in this record.

    Derived from the record itself rather than assumed, so the cohorts stay
    correct for any film reviewed across a change in the store.
    """
    splits, supersedes = [], []
    for entry in frames.values():
        review = entry.get("review") or {}
        if review.get("answer") == store.ANSWER_SPLIT:
            stamp = _stamp(review.get("timestamp"))
            if stamp:
                splits.append(stamp)
        for rec in (entry.get("manual") or {}).values():
            if "supersedes" in rec:
                stamp = _stamp(rec.get("timestamp"))
                if stamp:
                    supersedes.append(stamp)
    return {
        "split_live": min(splits).isoformat() if splits else None,
        "supersedes_live": min(supersedes).isoformat() if supersedes else None,
    }


def cohort_of(entry: dict, boundaries: dict) -> str:
    touched = frame_touched(entry)
    split_live = _stamp(boundaries.get("split_live"))
    supersedes_live = _stamp(boundaries.get("supersedes_live"))
    if touched is None:
        return COHORT_FULL
    if split_live and touched < split_live:
        return COHORT_PRE_SPLIT
    if supersedes_live and touched < supersedes_live:
        return COHORT_PRE_SUPERSEDES
    return COHORT_FULL


# ---------------------------------------------------------------------------
# Normalized per-shot record
# ---------------------------------------------------------------------------

def role_origins(entry: dict) -> dict:
    """Each final role's colour and where it came from, for every review shape.

    ``store.resolved_roles`` covers splits and manual picks but returns nothing
    for a whole-frame acceptance, whose provenance lives on ``final_palette``.
    Both are folded together here so figure and background can be counted
    independently regardless of how the reviewer arrived at them.
    """
    resolved = store.resolved_roles(entry)
    final = entry.get("final_palette") or {}
    manual = entry.get("manual") or {}
    origins = {}
    for role in store.ROLES:
        if role in resolved:
            record = dict(resolved[role])
        elif isinstance(final.get(role), dict):
            record = dict(final[role])
            record.setdefault("source", SOURCE_PROPOSAL)
            record["choice"] = final.get("choice")
            record["strategy"] = final.get("strategy")
        else:
            continue
        record["lab"] = record.get("lab") or to_lab(record.get("rgb"))
        if record.get("source") == SOURCE_MANUAL:
            record["supersedes"] = (manual.get(role) or {}).get("supersedes")
            record["pipette"] = bool((manual.get(role) or {}).get("pipette"))
        origins[role] = record
    return origins


def proposal_view(entry: dict) -> dict:
    """Every generated choice, flattened to what the analysis needs."""
    out = {}
    for key, choice in (entry.get("proposals") or {}).items():
        colours = choice.get("colours") or []
        measurement = choice.get("measurement") or []
        roles = {}
        for index, role in enumerate(store.ROLES):
            colour = colours[index] if index < len(colours) else None
            record = measurement[index] if index < len(measurement) else None
            segmentation = (record or {}).get("segmentation") or {}
            operational = (record or {}).get("operationalization") or {}
            roles[role] = {
                "colour": _colour_block(colour),
                "material": (record or {}).get("material"),
                "segmentation_status": segmentation.get("status"),
                "mask_pixels": segmentation.get("mask_pixels"),
                "coverage": segmentation.get("coverage"),
                "operationalization_status": operational.get("status"),
                "localization_targets": operational.get("localization_targets"),
            }
        out[key] = {
            "strategy": choice.get("strategy"),
            "label": choice.get("label"),
            "active": bool(choice.get("active")),
            "inactive_reason": choice.get("inactive_reason")
            or choice.get("convergence_reason"),
            "converges_with": choice.get("converges_with") or [],
            "source_stage": choice.get("source_stage"),
            "materials": choice.get("materials") or [],
            "roles": roles,
        }
    return out


def narratology_view(entry: dict) -> dict:
    """The frozen reading behind this frame's proposals."""
    generation = entry.get("proposal_generation") or {}
    reading = generation.get("narratology") or {}
    stands_out = [item for item in (reading.get("what_stands_out") or [])
                  if isinstance(item, dict)]
    first = stands_out[0] if stands_out else {}
    cue = reading.get("strongest_cue") or {}
    distinctions = [item for item in (reading.get("organizing_distinctions") or [])
                    if isinstance(item, dict)]
    return {
        "stands_out": first.get("entity"),
        "against": first.get("against"),
        "what_makes_it_stand_out": first.get("what_makes_it_stand_out"),
        "why_it_matters": first.get("why_it_matters"),
        "all_stands_out": [
            {"entity": item.get("entity"), "against": item.get("against")}
            for item in stands_out
        ],
        "cue": cue.get("visible_cue"),
        "spectator_inference": cue.get("spectator_inference"),
        "genre_context": cue.get("genre_context"),
        "critical_context": cue.get("critical_context"),
        "distinctions": [
            {
                "distinction": item.get("distinction"),
                "side_a": item.get("side_a"),
                "side_b": item.get("side_b"),
                "formal_carrier": item.get("formal_carrier"),
                "two_colour_potential": item.get("two_colour_potential"),
                "colour_role_a": item.get("colour_role_a"),
                "colour_role_b": item.get("colour_role_b"),
            }
            for item in distinctions
        ],
        "overall_reading": reading.get("overall_reading"),
        "uncertainty": reading.get("uncertainty"),
    }


def carrier_view(entry: dict) -> list:
    generation = entry.get("proposal_generation") or {}
    carrier = generation.get("carrier") or {}
    rows = []
    for item in carrier.get("carriers") or []:
        if not isinstance(item, dict):
            continue
        sides = {}
        for name in ("carrier_a", "carrier_b"):
            side = item.get(name)
            if isinstance(side, dict):
                sides[name] = {
                    "visible_material": side.get("visible_material"),
                    "qualitative_colour_or_tone": side.get("qualitative_colour_or_tone"),
                    "what_it_carries": side.get("what_it_carries"),
                }
        rows.append({
            "source_distinction": item.get("source_distinction"),
            "two_colour_strength": item.get("two_colour_strength"),
            "narratological_meaning": item.get("narratological_meaning"),
            "formal_carrier": item.get("formal_carrier"),
            **sides,
        })
    return rows


def articulation_view(entry: dict) -> list:
    generation = entry.get("proposal_generation") or {}
    articulation = generation.get("articulation") or {}
    rows = []
    for item in articulation.get("articulations") or []:
        if not isinstance(item, dict):
            continue
        pair = item.get("chromatic_articulation")
        pair = pair if isinstance(pair, dict) else {}
        rows.append({
            "source_distinction": item.get("source_distinction"),
            "two_colour_strength": item.get("two_colour_strength"),
            "relationship": pair.get("relationship"),
            "distinguished_material": pair.get("distinguished_material"),
            "counterfield_material": pair.get("counterfield_material"),
            "why_this_pair": pair.get("why_this_pair"),
        })
    return rows


def normalized_records(project_path: str, filename: str, media_type: str) -> list:
    """One record per reviewed shot, in shot order, losing no distinction."""
    from services.palette_review import list_frames

    record = store.load_review(project_path, filename, media_type)
    frames = record.get("frames") or {}
    boundaries = feature_boundaries(frames)
    order = {item["shot_id"]: item for item in
             list_frames(project_path, filename, media_type)}

    rows = []
    for shot_id, entry in frames.items():
        position = order.get(shot_id) or {}
        touched = frame_touched(entry)
        rows.append({
            "index": position.get("index"),
            "shot_id": shot_id,
            "scene": position.get("scene"),
            "start_time": position.get("start_time"),
            "frame_image": position.get("image")
            or (entry.get("proposal_generation") or {}).get("source_image"),
            "state": store.frame_state(entry),
            "cohort": cohort_of(entry, boundaries),
            "reviewed_at": touched.isoformat() if touched else None,
            "review_answer": (entry.get("review") or {}).get("answer"),
            "generation_status": (entry.get("proposal_generation") or {}).get("status"),
            "roles": role_origins(entry),
            "proposals": proposal_view(entry),
            "narratology": narratology_view(entry),
            "carrier": carrier_view(entry),
            "articulation": articulation_view(entry),
        })
    rows.sort(key=lambda item: (item["index"] is None, item["index"], item["shot_id"]))
    return rows, boundaries
