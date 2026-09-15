"""E7 relational figure/ground evidence over the frozen E5/E6 candidate pool.

E7 treats the *ordered pair* as the unit. It never scores a candidate
independently and then combines two winners; every signal below is a property
of a relation ``(figure, ground)``.

Everything here is model-free. It reads the frozen E6 candidate records and
persisted masks plus the frozen E5 inventory, and recomputes only the
deterministic letterbox-crop/resize needed to measure local contrast. No Qwen,
no SAM3, no rerun of E4/E5/E6.

Normalisation follows E5's own discipline: per-image min-max across all pairs,
then an equal-weight mean within each evidence family. Nothing is weighted per
image and nothing is thresholded.
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from data.palette import _rgb_to_lab


# Figure/ground is a neighbourhood relation, not a strict border relation: in
# PAL-015 the human figure and ground share zero touching pixels.
RING_RADIUS = 8
CONTACT_RADIUS = 1
HUE_MIN_CHROMA = 5.0

FORMAL_SIGNALS = ("delta_e", "delta_lightness", "delta_chroma", "local_delta_e")
SPATIAL_SIGNALS = (
    "union_coverage", "spatial_complement", "enclosure",
    "relative_scale", "figure_compactness",
)
SEMANTIC_SIGNALS = (
    "figure_lens_agreement", "ground_lens_agreement", "lens_disjointness",
    "figure_cross_source", "ground_cross_source",
)
HYPOTHESES = ("R1_additive", "R2_conjunctive", "R3_bottleneck")


def ring(mask: np.ndarray, radius: int) -> np.ndarray:
    """Return the dilation shell around a mask, excluding the mask itself."""
    grown = mask.copy()
    for _ in range(radius):
        step = grown.copy()
        step[1:, :] |= grown[:-1, :]
        step[:-1, :] |= grown[1:, :]
        step[:, 1:] |= grown[:, :-1]
        step[:, :-1] |= grown[:, 1:]
        grown = step
    return grown & ~mask


def _chroma(lab) -> float:
    return math.hypot(float(lab[1]), float(lab[2]))


def delta_e(left_lab, right_lab) -> float:
    """Plain CIE76 distance, reported as a raw measurement with no threshold."""
    return round(math.dist([float(v) for v in left_lab], [float(v) for v in right_lab]), 3)


def hue_relation(figure_lab, ground_lab) -> dict:
    """Report hue opposition only where both colours actually have a hue."""
    if min(_chroma(figure_lab), _chroma(ground_lab)) < HUE_MIN_CHROMA:
        return {"available": False,
                "reason": "at least one colour is near-neutral; hue is undefined"}
    figure_hue = math.degrees(math.atan2(float(figure_lab[2]), float(figure_lab[1]))) % 360.0
    ground_hue = math.degrees(math.atan2(float(ground_lab[2]), float(ground_lab[1]))) % 360.0
    difference = abs(figure_hue - ground_hue)
    difference = min(difference, 360.0 - difference)
    return {
        "available": True,
        "figure_hue_degrees": round(figure_hue, 1),
        "ground_hue_degrees": round(ground_hue, 1),
        "hue_difference_degrees": round(difference, 1),
        "opposition": round(1.0 - abs(180.0 - difference) / 180.0, 4),
    }


def colour_multiplicity(records: list[dict]) -> dict[str, int]:
    """Count candidates sharing an identical measured colour.

    Kept strictly out of scoring: a region must not become stronger merely
    because several E4 lenses produced near-identical records of it.
    """
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[record["hex"]] += 1
    return {record["candidate_id"]: counts[record["hex"]] for record in records}


def lens_support(records: list[dict], masks: dict[str, np.ndarray]) -> dict[str, dict[str, float]]:
    """Measure how strongly each distinct E4 lens independently nominates a region.

    Support for a lens is the best mask IoU between this candidate and any
    candidate that lens produced. It is continuous and unthresholded, so it
    separates *independent agreement between lenses* from bare duplication.
    """
    by_lens: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if record["source"] == "semantic" and record["semantic_role"]:
            by_lens[record["semantic_role"]].append(record)

    support: dict[str, dict[str, float]] = {}
    for record in records:
        mask = masks[record["candidate_id"]]
        area = int(mask.sum())
        per_lens = {}
        for lens, others in by_lens.items():
            best = 0.0
            for other in others:
                if other["candidate_id"] == record["candidate_id"]:
                    best = max(best, 1.0)
                    continue
                other_mask = masks[other["candidate_id"]]
                union = int((mask | other_mask).sum())
                if union:
                    best = max(best, int((mask & other_mask).sum()) / union)
            if best > 0:
                per_lens[lens] = round(best, 4)
        support[record["candidate_id"]] = per_lens
        record["_lens_support"] = per_lens
        record["_area"] = area
    return support


def _compactness(mask: np.ndarray) -> float:
    rows, cols = np.nonzero(mask)
    if not len(rows):
        return 0.0
    box = (rows.max() - rows.min() + 1) * (cols.max() - cols.min() + 1)
    return float(mask.sum()) / float(box) if box else 0.0


def prepare_geometry(records: list[dict], masks: dict[str, np.ndarray]) -> dict:
    """Precompute the per-candidate geometry every pair signal reuses."""
    geometry = {}
    for record in records:
        candidate_id = record["candidate_id"]
        mask = masks[candidate_id]
        geometry[candidate_id] = {
            "mask": mask,
            "area": int(mask.sum()),
            "coverage": float(mask.mean()),
            "ring": ring(mask, RING_RADIUS),
            "contact_ring": ring(mask, CONTACT_RADIUS),
            "compactness": round(_compactness(mask), 4),
        }
    return geometry


def local_colour(arr_rgb: np.ndarray, mask: np.ndarray) -> list[float] | None:
    """Measure the LAB mean of an arbitrary region, or None when it is empty."""
    if not mask.any():
        return None
    pixels = arr_rgb[mask]
    rgb = np.rint(pixels.astype(np.float64).mean(axis=0)).astype(np.uint8)
    return [round(float(value), 1) for value in _rgb_to_lab(rgb.reshape(1, 3))[0]]


def pair_evidence(figure: dict, ground: dict, geometry: dict,
                  arr_rgb: np.ndarray | None, cross_source: dict) -> dict:
    """Build the complete relational evidence vector for one ordered pair.

    The vector is preserved whole. No signal is collapsed into a score here.
    """
    figure_id, ground_id = figure["candidate_id"], ground["candidate_id"]
    figure_geometry, ground_geometry = geometry[figure_id], geometry[ground_id]
    figure_mask, ground_mask = figure_geometry["mask"], ground_geometry["mask"]

    shared = int((figure_mask & ground_mask).sum())
    union = int((figure_mask | ground_mask).sum())
    iou = shared / union if union else 0.0
    figure_ring = figure_geometry["ring"]
    ring_area = int(figure_ring.sum())
    enclosure = int((figure_ring & ground_mask).sum()) / ring_area if ring_area else 0.0
    reverse_ring = ground_geometry["ring"]
    reverse_area = int(reverse_ring.sum())
    reverse_enclosure = (
        int((reverse_ring & figure_mask).sum()) / reverse_area if reverse_area else 0.0
    )
    contact = int((figure_geometry["contact_ring"] & ground_mask).sum())

    figure_lab = [float(value) for value in figure["lab"]]
    ground_lab = [float(value) for value in ground["lab"]]
    neighbourhood = figure_ring & ground_mask
    local_lab = local_colour(arr_rgb, neighbourhood) if arr_rgb is not None else None
    local_delta_e = (
        round(math.dist(figure_lab, local_lab), 3) if local_lab is not None else None
    )

    figure_lenses = set(figure.get("_lens_support") or {})
    ground_lenses = set(ground.get("_lens_support") or {})
    lens_union = figure_lenses | ground_lenses
    coverage_sum = figure_geometry["coverage"] + ground_geometry["coverage"]

    return {
        "figure_id": figure_id,
        "ground_id": ground_id,
        "formal": {
            "delta_e": round(math.dist(figure_lab, ground_lab), 3),
            "delta_lightness": round(abs(figure_lab[0] - ground_lab[0]), 3),
            "delta_chroma": round(abs(_chroma(figure_lab) - _chroma(ground_lab)), 3),
            "local_delta_e": local_delta_e,
            "local_sample_pixels": int(neighbourhood.sum()),
            "hue_relation": hue_relation(figure_lab, ground_lab),
        },
        "spatial": {
            "figure_coverage": round(figure_geometry["coverage"], 5),
            "ground_coverage": round(ground_geometry["coverage"], 5),
            "union_coverage": round(union / figure_mask.size, 5),
            "overlap_iou": round(iou, 4),
            "spatial_complement": round(1.0 - iou, 4),
            "enclosure": round(enclosure, 4),
            "reverse_enclosure": round(reverse_enclosure, 4),
            "relative_scale": round(
                ground_geometry["coverage"] / coverage_sum if coverage_sum else 0.0, 4
            ),
            "figure_compactness": figure_geometry["compactness"],
            "ground_compactness": ground_geometry["compactness"],
            "contact_pixels": contact,
            "centroid_distance": round(
                math.dist(figure["centroid"], ground["centroid"]), 4
            ),
        },
        "semantic": {
            "figure_lens_support": figure.get("_lens_support") or {},
            "ground_lens_support": ground.get("_lens_support") or {},
            "figure_lens_agreement": round(
                sum((figure.get("_lens_support") or {}).values()), 4
            ),
            "ground_lens_agreement": round(
                sum((ground.get("_lens_support") or {}).values()), 4
            ),
            "lens_disjointness": round(
                1.0 - len(figure_lenses & ground_lenses) / len(lens_union), 4
            ) if lens_union else 0.0,
            "figure_source": figure["source"],
            "ground_source": ground["source"],
            "figure_label": figure["label"],
            "ground_label": ground["label"],
        },
        "structural": {
            "figure_cross_source": cross_source.get(figure_id, 0.0),
            "ground_cross_source": cross_source.get(ground_id, 0.0),
            "figure_hierarchy_level": figure.get("hierarchy_level"),
            "ground_hierarchy_level": ground.get("hierarchy_level"),
        },
        "multiplicity": {
            "figure_colour_multiplicity": figure.get("_multiplicity"),
            "ground_colour_multiplicity": ground.get("_multiplicity"),
            "note": "recorded for inspection only; never scored",
        },
    }


def _flatten(vector: dict) -> dict[str, float | None]:
    flat = {}
    for name in FORMAL_SIGNALS:
        flat[name] = vector["formal"].get(name)
    for name in SPATIAL_SIGNALS:
        flat[name] = vector["spatial"].get(name)
    for name in SEMANTIC_SIGNALS:
        source = vector["semantic"] if name in vector["semantic"] else vector["structural"]
        flat[name] = source.get(name)
    return flat


def normalise(values: list[float | None]) -> list[float]:
    """Per-image min-max, matching E5. Unavailable signals become neutral."""
    present = [value for value in values if value is not None]
    if not present:
        return [0.5] * len(values)
    low, high = min(present), max(present)
    if math.isclose(low, high):
        return [0.5] * len(values)
    return [
        0.5 if value is None else round((value - low) / (high - low), 4)
        for value in values
    ]


def score_families(vectors: list[dict]) -> list[dict]:
    """Normalise every signal across the image's pairs, then average per family.

    Families are equal-weight and globally specified. Nothing is tuned per
    image, and an unavailable signal is neutral rather than invented.
    """
    flats = [_flatten(vector) for vector in vectors]
    columns = {
        name: normalise([flat[name] for flat in flats])
        for name in FORMAL_SIGNALS + SPATIAL_SIGNALS + SEMANTIC_SIGNALS
    }
    scored = []
    for index, vector in enumerate(vectors):
        families = {}
        for family, names in (
            ("formal", FORMAL_SIGNALS),
            ("spatial", SPATIAL_SIGNALS),
            ("semantic", SEMANTIC_SIGNALS),
        ):
            parts = {name: columns[name][index] for name in names}
            families[family] = {
                "signals": parts,
                "score": round(sum(parts.values()) / len(parts), 4),
            }
        formal = families["formal"]["score"]
        spatial = families["spatial"]["score"]
        semantic = families["semantic"]["score"]
        scored.append({
            **vector,
            "families": families,
            "hypotheses": {
                "R1_additive": round((formal + spatial + semantic) / 3.0, 4),
                "R2_conjunctive": round((formal * spatial * semantic) ** (1 / 3), 4),
                "R3_bottleneck": round(min(formal, spatial, semantic), 4),
            },
        })
    return scored


def rank_pairs(scored: list[dict], hypothesis: str) -> list[dict]:
    """Order pairs by one hypothesis; ties break on id for reproducibility."""
    return sorted(
        scored,
        key=lambda pair: (
            -pair["hypotheses"][hypothesis], pair["figure_id"], pair["ground_id"]
        ),
    )


def rank_of(scored: list[dict], hypothesis: str, figure_id: str, ground_id: str) -> dict:
    ordered = rank_pairs(scored, hypothesis)
    for position, pair in enumerate(ordered, start=1):
        if pair["figure_id"] == figure_id and pair["ground_id"] == ground_id:
            return {
                "rank": position,
                "of": len(ordered),
                "score": pair["hypotheses"][hypothesis],
            }
    return {"rank": None, "of": len(ordered), "score": None}


def distinct_colour_pairs(ordered: list[dict], by_id: dict, limit: int) -> list[dict]:
    """Collapse presented results that are the same two measured colours.

    Scores are untouched. This only stops one region nominated by five lenses
    from filling a top-N list and hiding genuinely different relations.
    """
    first: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for position, pair in enumerate(ordered, start=1):
        key = (by_id[pair["figure_id"]]["hex"], by_id[pair["ground_id"]]["hex"])
        if key in first:
            first[key]["represents"] += 1
            continue
        first[key] = {**pair, "rank": position, "represents": 1}
        order.append(key)
    return [first[key] for key in order[:limit]]


def build_pairs(records: list[dict], masks: dict[str, np.ndarray],
                arr_rgb: np.ndarray | None, cross_source: dict) -> list[dict]:
    """Evaluate every ordered pair of the frozen pool as a relation."""
    lens_support(records, masks)
    multiplicity = colour_multiplicity(records)
    for record in records:
        record["_multiplicity"] = multiplicity[record["candidate_id"]]
    geometry = prepare_geometry(records, masks)
    vectors = [
        pair_evidence(figure, ground, geometry, arr_rgb, cross_source)
        for figure in records
        for ground in records
        if figure["candidate_id"] != ground["candidate_id"]
    ]
    return score_families(vectors)
