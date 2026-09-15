"""E5 two-colour reduction over frozen E4 evidence.

This is intentionally an experimental, inspectable reducer.  It does not
write project state, invoke Qwen, or alter E3/E4.  Each output colour is the
mean of an explicitly measured semantic mask or an E4 hierarchy region.

The reducer uses an equal-weight evidence baseline rather than treating its
scores as a finished palette theory.  Its job is to expose useful signals and
disagreements for inspection, not to hide them behind a learned answer.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from data.palette import _rgb_to_lab


WORKING = 256
ROLE_ORDER = (
    "narrative",
    "compositional",
    "chromatic",
    "luminance",
    "iconographic",
    "atmospheric",
    "graphic",
)
_EXTENT_VALUE = {"small": 0.0, "medium": 0.5, "large": 1.0}


def rgb_hex(rgb: list[int]) -> str:
    """Return a stable hexadecimal representation for an RGB triplet."""
    return "#{:02x}{:02x}{:02x}".format(*[int(value) for value in rgb])


def _mask_geometry(mask: np.ndarray) -> dict:
    rows, cols = np.nonzero(mask)
    height, width = mask.shape
    return {
        "area": int(mask.sum()),
        "coverage": round(float(mask.mean()), 5),
        "centroid": [
            round(float(cols.mean()) / width, 4),
            round(float(rows.mean()) / height, 4),
        ],
        "bbox": [int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())],
    }


def _measure_mask(arr_rgb: np.ndarray, mask: np.ndarray) -> dict:
    pixels = arr_rgb[mask]
    rgb = np.rint(pixels.astype(np.float64).mean(axis=0)).astype(np.uint8)
    lab = _rgb_to_lab(rgb.reshape(1, 3))[0]
    return {
        "rgb": rgb.tolist(),
        "hex": rgb_hex(rgb.tolist()),
        "lab": [round(float(value), 1) for value in lab],
        "lightness": round(float(lab[0]), 1),
        "chroma": round(float(math.hypot(lab[1], lab[2])), 1),
    }


def _mask_overlap(left: np.ndarray, right: np.ndarray) -> dict:
    shared = int(np.count_nonzero(left & right))
    left_count = max(1, int(left.sum()))
    right_count = max(1, int(right.sum()))
    union = left_count + right_count - shared
    return {
        "shared_pixels": shared,
        "share_of_candidate": round(shared / left_count, 4),
        "share_of_other": round(shared / right_count, 4),
        "iou": round(shared / max(1, union), 4),
    }


def recover_hierarchy_masks(
    hierarchy: dict,
    artifact_dir: Path,
    *,
    working_size: int = WORKING,
) -> tuple[dict[int, dict[int, np.ndarray]], dict]:
    """Recover E4 hierarchy masks from its exact-colour PNG snapshots.

    E4 persisted each region's measured colour and rendered it losslessly into
    a nearest-neighbour PNG.  Region colours were checked to be unique for the
    pilot, so the snapshot is sufficient to recover a mask for pairwise E5
    comparison without re-running the E4 hierarchy.
    """
    recovered: dict[int, dict[int, np.ndarray]] = {}
    report = {"method": "exact_region_rgb_from_e4_png_then_nearest_to_working", "levels": {}}

    for level_text, record in sorted(hierarchy.items(), key=lambda item: -int(item[0])):
        level = int(level_text)
        path = artifact_dir / f"04-hierarchy-{level:02d}.png"
        with Image.open(path) as opened:
            pixels = np.asarray(opened.convert("RGB"))

        masks: dict[int, np.ndarray] = {}
        covered = np.zeros(pixels.shape[:2], dtype=bool)
        regions = record.get("regions") or []
        for region in regions:
            rgb = np.asarray(region["rgb"], dtype=np.uint8)
            display_mask = np.all(pixels == rgb, axis=2)
            working_mask = np.asarray(
                Image.fromarray(display_mask.astype(np.uint8) * 255, mode="L").resize(
                    (working_size, working_size), Image.Resampling.NEAREST
                )
            ) > 127
            masks[int(region["index"])] = working_mask
            covered |= display_mask

        recovered[level] = masks
        report["levels"][str(level)] = {
            "artifact": path.name,
            "regions_expected": len(regions),
            "regions_recovered": len(masks),
            "unmapped_snapshot_pixels": int((~covered).sum()),
            "working_union_coverage": round(
                float(np.logical_or.reduce(list(masks.values())).mean()) if masks else 0.0,
                5,
            ),
        }
    return recovered, report


def hierarchy_candidates(
    hierarchy: dict,
    recovered_masks: dict[int, dict[int, np.ndarray]],
    residual: dict | None = None,
) -> list[dict]:
    """Make one candidate per frozen E4 hierarchy region at every scale."""
    residual = residual or {}
    candidates = []
    for level_text, record in sorted(hierarchy.items(), key=lambda item: -int(item[0])):
        level = int(level_text)
        residual_by_region = {
            int(row["region"]): row
            for row in residual.get("levels", {}).get(str(level), [])
        }
        for region in record.get("regions") or []:
            region_id = int(region["index"])
            candidates.append({
                "candidate_id": f"hierarchy:L{level}:R{region_id}",
                "source": "hierarchy",
                "available": True,
                "rgb": [int(value) for value in region["rgb"]],
                "hex": region["hex"],
                "lab": region["lab"],
                "lightness": region["lightness"],
                "chroma": region["chroma"],
                "area": int(region["pixel_count"]),
                "coverage": region["coverage"],
                "centroid": region["centroid"],
                "bbox": region["bbox"],
                "provenance": {
                    "source": "hierarchy",
                    "level": level,
                    "region_identity": region_id,
                    "region_adjacency": region.get("adjacent", []),
                    "contiguous_pieces": region.get("contiguous_pieces"),
                    "recovered_from": f"04-hierarchy-{level:02d}.png",
                },
                "hierarchy": {
                    "level": level,
                    "region_identity": region_id,
                    "pixel_count": int(region["pixel_count"]),
                    "residual_fraction": residual_by_region.get(region_id, {}).get("residual_fraction"),
                },
                "semantic_roles": [],
                "_mask": recovered_masks[level][region_id],
            })
    return candidates


def replay_e4_phrases(segmenter, image, inventory: dict) -> tuple[dict[str, list[dict]], dict]:
    """Replay only phrases that E4 originally spatialized, once each.

    The E4 JSON deliberately omitted raw binary masks.  Replaying its frozen
    successful phrases gives E5 pixels to measure while the E4 role, phrase,
    mask index, and geometry remain the authoritative provenance record.
    """
    from scripts.palette_lab.e4_pilot import _segment

    phrases = sorted({
        str(mask.get("phrase") or "").strip()
        for role in (inventory.get("semantic_roles") or {}).values()
        for mask in (role.get("masks") or [])
        if str(mask.get("phrase") or "").strip()
    })
    replayed = {}
    report = {"phrases_requested": phrases, "phrases": {}}
    for phrase in phrases:
        masks = _segment(segmenter, image, phrase)
        replayed[phrase] = masks
        report["phrases"][phrase] = {
            "replayed_mask_count": sum(1 for mask in masks if isinstance(mask.get("mask"), np.ndarray)),
            "errors": [mask["error"] for mask in masks if mask.get("error")],
        }
    return replayed, report


def semantic_candidates(
    inventory: dict,
    replayed_masks: dict[str, list[dict]],
    arr_rgb: np.ndarray,
) -> tuple[list[dict], list[dict]]:
    """Measure one colour per E4 semantic instance without erasing failures."""
    candidates: list[dict] = []
    unavailable: list[dict] = []
    role_records = inventory.get("semantic_roles") or {}
    ordered_roles = [role for role in ROLE_ORDER if role in role_records]
    ordered_roles.extend(sorted(set(role_records) - set(ordered_roles)))

    for role in ordered_roles:
        reading = role_records[role]
        for stored in reading.get("masks") or []:
            phrase = str(stored.get("phrase") or "")
            mask_index = int(stored.get("mask_index", -1))
            replay = next(
                (item for item in replayed_masks.get(phrase, []) if item.get("mask_index") == mask_index),
                None,
            )
            candidate_id = f"semantic:{role}:{phrase}:{mask_index}"
            provenance = {
                "source": "semantic",
                "semantic_role": role,
                "interpretation": reading.get("interpretation", ""),
                "colour_language": reading.get("color_language", ""),
                "segment_phrase": phrase,
                "e4_mask_index": mask_index,
                "declared_location": reading.get("location", ""),
                "declared_extent": reading.get("extent", ""),
                "e4_mask_geometry": {
                    key: stored.get(key)
                    for key in ("area", "relative_area", "centroid", "bbox", "score", "distance_to_declared", "inside_declared_cell")
                },
                "e4_role_selection": {
                    key: reading.get(key)
                    for key in ("best_by_location", "largest_mask", "deixis_agrees_with_size")
                },
                "frozen_hierarchy_overlap": reading.get("hierarchy_overlap", {}),
            }
            if replay is None or not isinstance(replay.get("mask"), np.ndarray):
                unavailable.append({
                    "candidate_id": candidate_id,
                    "source": "semantic",
                    "available": False,
                    "provenance": provenance,
                    "reason": "E5 phrase replay did not reproduce the stored E4 mask index",
                })
                continue

            mask = replay["mask"]
            geometry = _mask_geometry(mask)
            stored_centroid = stored.get("centroid") or [None, None]
            replay_centroid = geometry["centroid"]
            centroid_drift = None
            if all(value is not None for value in stored_centroid):
                centroid_drift = round(math.dist(stored_centroid, replay_centroid), 4)
            stored_area = int(stored.get("area") or 0)
            candidates.append({
                "candidate_id": candidate_id,
                "source": "semantic",
                "available": True,
                **_measure_mask(arr_rgb, mask),
                **geometry,
                "provenance": provenance,
                "semantic": {
                    "role": role,
                    "segment_phrase": phrase,
                    "mask_index": mask_index,
                    "sam_score": replay.get("score"),
                    "distance_to_declared": stored.get("distance_to_declared"),
                    "inside_declared_cell": stored.get("inside_declared_cell"),
                    "declared_extent": reading.get("extent", ""),
                    "replay": {
                        "method": "phrase_plus_e4_mask_index",
                        "area_delta_pixels": geometry["area"] - stored_area,
                        "area_delta_fraction": round(
                            (geometry["area"] - stored_area) / max(1, stored_area), 4
                        ),
                        "centroid_drift": centroid_drift,
                    },
                },
                "semantic_roles": [role],
                "_mask": mask,
            })

    for role in ordered_roles:
        reading = role_records[role]
        if reading.get("mask_count", 0):
            continue
        unavailable.append({
            "candidate_id": f"semantic-reading:{role}",
            "source": "semantic",
            "available": False,
            "provenance": {
                "source": "semantic",
                "semantic_role": role,
                "interpretation": reading.get("interpretation", ""),
                "colour_language": reading.get("color_language", ""),
                "segment_phrases": reading.get("segment_phrases", []),
                "declared_location": reading.get("location", ""),
                "declared_extent": reading.get("extent", ""),
            },
            "reason": "E4 produced no measurable instance masks for this reading",
        })
    return candidates, unavailable


def add_cross_source_evidence(candidates: list[dict]) -> None:
    """Attach all semantic/hierarchy overlaps and same-source role consensus."""
    semantic = [candidate for candidate in candidates if candidate["source"] == "semantic"]
    hierarchy = [candidate for candidate in candidates if candidate["source"] == "hierarchy"]

    role_iou: dict[str, dict[str, float]] = defaultdict(dict)
    for left_index, left in enumerate(semantic):
        for right in semantic[left_index + 1:]:
            if left["semantic"]["role"] == right["semantic"]["role"]:
                continue
            overlap = _mask_overlap(left["_mask"], right["_mask"])
            if overlap["shared_pixels"]:
                role_iou[left["candidate_id"]][right["semantic"]["role"]] = max(
                    role_iou[left["candidate_id"]].get(right["semantic"]["role"], 0.0), overlap["iou"]
                )
                role_iou[right["candidate_id"]][left["semantic"]["role"]] = max(
                    role_iou[right["candidate_id"]].get(left["semantic"]["role"], 0.0), overlap["iou"]
                )

    hierarchy_role_support: dict[str, dict[str, float]] = defaultdict(dict)
    for semantic_candidate in semantic:
        overlaps = []
        for hierarchy_candidate in hierarchy:
            overlap = _mask_overlap(semantic_candidate["_mask"], hierarchy_candidate["_mask"])
            if not overlap["shared_pixels"]:
                continue
            item = {
                "candidate_id": hierarchy_candidate["candidate_id"],
                "level": hierarchy_candidate["hierarchy"]["level"],
                "region_identity": hierarchy_candidate["hierarchy"]["region_identity"],
                **overlap,
            }
            overlaps.append(item)
            role = semantic_candidate["semantic"]["role"]
            hierarchy_role_support[hierarchy_candidate["candidate_id"]][role] = max(
                hierarchy_role_support[hierarchy_candidate["candidate_id"]].get(role, 0.0),
                overlap["share_of_other"],
            )
        overlaps.sort(key=lambda item: (-item["iou"], -item["share_of_candidate"], item["candidate_id"]))
        semantic_candidate["cross_source"] = {
            "hierarchy_overlaps": overlaps,
            "best_overlap_iou": overlaps[0]["iou"] if overlaps else 0.0,
            "mutual_majority": any(
                item["share_of_candidate"] >= 0.5 and item["share_of_other"] >= 0.5
                for item in overlaps
            ),
        }
        semantic_candidate["semantic_consensus"] = {
            "role_iou": role_iou[semantic_candidate["candidate_id"]],
            "roles_with_overlap": sorted(role_iou[semantic_candidate["candidate_id"]]),
        }

    for hierarchy_candidate in hierarchy:
        reverse = []
        for semantic_candidate in semantic:
            overlap = _mask_overlap(hierarchy_candidate["_mask"], semantic_candidate["_mask"])
            if not overlap["shared_pixels"]:
                continue
            reverse.append({
                "candidate_id": semantic_candidate["candidate_id"],
                "semantic_role": semantic_candidate["semantic"]["role"],
                "segment_phrase": semantic_candidate["semantic"]["segment_phrase"],
                **overlap,
            })
        reverse.sort(key=lambda item: (-item["iou"], -item["share_of_candidate"], item["candidate_id"]))
        support = hierarchy_role_support[hierarchy_candidate["candidate_id"]]
        hierarchy_candidate["semantic_roles"] = sorted(support)
        hierarchy_candidate["cross_source"] = {
            "semantic_overlaps": reverse,
            "semantic_role_support": support,
            "best_overlap_iou": reverse[0]["iou"] if reverse else 0.0,
            "mutual_majority": any(
                item["share_of_candidate"] >= 0.5 and item["share_of_other"] >= 0.5
                for item in reverse
            ),
        }


def add_hierarchy_persistence(candidates: list[dict]) -> None:
    """Record visible cross-level containment without claiming stored lineage."""
    hierarchy = [candidate for candidate in candidates if candidate["source"] == "hierarchy"]
    levels = sorted({candidate["hierarchy"]["level"] for candidate in hierarchy}, reverse=True)
    for candidate in hierarchy:
        level = candidate["hierarchy"]["level"]
        coarser = [other_level for other_level in levels if other_level < level]
        matches = []
        for other_level in coarser:
            possibilities = [
                other for other in hierarchy
                if other["hierarchy"]["level"] == other_level
            ]
            best = max(
                (_mask_overlap(candidate["_mask"], other["_mask"]) | {
                    "candidate_id": other["candidate_id"], "level": other_level,
                } for other in possibilities),
                key=lambda item: (item["share_of_candidate"], item["iou"]),
                default=None,
            )
            if best is not None:
                matches.append(best)
        candidate["hierarchy"]["cross_level_containment"] = matches
        candidate["hierarchy"]["persistence"] = round(
            sum(item["share_of_candidate"] for item in matches) / len(matches), 4
        ) if matches else 1.0


def _normalise(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    low, high = min(values.values()), max(values.values())
    if math.isclose(low, high):
        return {key: 0.5 for key in values}
    return {key: round((value - low) / (high - low), 4) for key, value in values.items()}


def _chromatic_relation(left_lab: list[float], right_lab: list[float]) -> dict:
    """Report hue opposition when both colours are chromatic enough to have hue."""
    left_chroma = math.hypot(float(left_lab[1]), float(left_lab[2]))
    right_chroma = math.hypot(float(right_lab[1]), float(right_lab[2]))
    if min(left_chroma, right_chroma) < 5.0:
        return {
            "available": False,
            "reason": "at least one colour is near-neutral; hue opposition is undefined",
        }
    left_hue = math.degrees(math.atan2(float(left_lab[2]), float(left_lab[1]))) % 360.0
    right_hue = math.degrees(math.atan2(float(right_lab[2]), float(right_lab[1]))) % 360.0
    difference = abs(left_hue - right_hue)
    difference = min(difference, 360.0 - difference)
    return {
        "available": True,
        "focus_hue_degrees": round(left_hue, 1),
        "ambiance_hue_degrees": round(right_hue, 1),
        "hue_difference_degrees": round(difference, 1),
        "opposition": round(1.0 - abs(180.0 - difference) / 180.0, 4),
    }


def _candidate_roles(candidate: dict) -> set[str]:
    return set(candidate.get("semantic_roles") or [])


def _deixis_support(candidate: dict, by_id: dict[str, dict]) -> float:
    if candidate["source"] == "semantic":
        distance = candidate["semantic"].get("distance_to_declared")
        return 0.5 if distance is None else max(0.0, 1.0 - float(distance) / math.sqrt(2))
    best = 0.5
    for item in candidate.get("cross_source", {}).get("semantic_overlaps", []):
        semantic = by_id[item["candidate_id"]]
        distance = semantic["semantic"].get("distance_to_declared")
        if distance is None:
            continue
        location_score = max(0.0, 1.0 - float(distance) / math.sqrt(2))
        best = max(best, location_score * item["share_of_candidate"])
    return best


def _semantic_context(candidate: dict, by_id: dict[str, dict]) -> float:
    if candidate["source"] == "semantic":
        return _EXTENT_VALUE.get(str(candidate["semantic"].get("declared_extent", "")).lower(), 0.5)
    values = []
    for item in candidate.get("cross_source", {}).get("semantic_overlaps", []):
        semantic = by_id[item["candidate_id"]]
        extent = _EXTENT_VALUE.get(str(semantic["semantic"].get("declared_extent", "")).lower(), 0.5)
        values.append(extent * item["share_of_candidate"])
    return max(values, default=0.0)


def _coarse_scale(candidate: dict, by_id: dict[str, dict]) -> float:
    if candidate["source"] == "hierarchy":
        level = candidate["hierarchy"]["level"]
        return 1.0 - math.log(level / 3.0) / math.log(12.0 / 3.0)
    values = []
    for item in candidate.get("cross_source", {}).get("hierarchy_overlaps", []):
        hierarchy = by_id[item["candidate_id"]]
        values.append(_coarse_scale(hierarchy, by_id) * item["share_of_candidate"])
    return max(values, default=0.0)


def _persistence(candidate: dict, by_id: dict[str, dict]) -> float:
    if candidate["source"] == "hierarchy":
        return float(candidate["hierarchy"].get("persistence", 0.0))
    values = []
    for item in candidate.get("cross_source", {}).get("hierarchy_overlaps", []):
        hierarchy = by_id[item["candidate_id"]]
        values.append(_persistence(hierarchy, by_id) * item["share_of_candidate"])
    return max(values, default=0.0)


def _residual_field(candidate: dict, by_id: dict[str, dict]) -> float:
    if candidate["source"] == "hierarchy":
        return float(candidate["hierarchy"].get("residual_fraction") or 0.0)
    values = []
    for item in candidate.get("cross_source", {}).get("hierarchy_overlaps", []):
        hierarchy = by_id[item["candidate_id"]]
        values.append(_residual_field(hierarchy, by_id) * item["share_of_candidate"])
    return max(values, default=0.0)


def _sam_confidence(candidate: dict, by_id: dict[str, dict]) -> float:
    if candidate["source"] == "semantic":
        return float(candidate["semantic"].get("sam_score") or 0.0)
    values = []
    for item in candidate.get("cross_source", {}).get("semantic_overlaps", []):
        semantic = by_id[item["candidate_id"]]
        values.append(_sam_confidence(semantic, by_id) * item["share_of_candidate"])
    return max(values, default=0.0)


def _visual_salience(candidate: dict, chroma: float, lightness_deviation: float) -> float:
    """Use the E4 lens before combining chromatic and luminance salience."""
    if candidate["source"] == "semantic":
        role = candidate["semantic"]["role"]
        if role == "chromatic":
            return chroma
        if role == "luminance":
            return lightness_deviation
    return max(chroma, lightness_deviation)


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """Attach role-specific equal-weight evidence scores to every candidate."""
    selectable = [candidate for candidate in candidates if candidate.get("available")]
    by_id = {candidate["candidate_id"]: candidate for candidate in selectable}
    coverage = _normalise({candidate["candidate_id"]: math.sqrt(candidate["coverage"]) for candidate in selectable})
    chroma = _normalise({candidate["candidate_id"]: float(candidate["chroma"]) for candidate in selectable})
    lightness = [float(candidate["lightness"]) for candidate in selectable]
    median_lightness = float(np.median(lightness)) if lightness else 0.0
    deviation = _normalise({
        candidate["candidate_id"]: abs(float(candidate["lightness"]) - median_lightness)
        for candidate in selectable
    })
    active_roles = {
        candidate["semantic"]["role"] for candidate in selectable
        if candidate["source"] == "semantic"
    }

    for candidate in selectable:
        candidate_id = candidate["candidate_id"]
        if candidate["source"] == "semantic":
            attention = 1.0
            consensus = len(candidate.get("semantic_consensus", {}).get("roles_with_overlap", [])) / max(1, len(active_roles) - 1)
            alignment = candidate.get("cross_source", {}).get("best_overlap_iou", 0.0)
        else:
            overlaps = candidate.get("cross_source", {}).get("semantic_overlaps", [])
            attention = max((item["share_of_candidate"] for item in overlaps), default=0.0)
            consensus = len(_candidate_roles(candidate)) / max(1, len(active_roles))
            alignment = candidate.get("cross_source", {}).get("best_overlap_iou", 0.0)

        visual_salience = _visual_salience(
            candidate, chroma[candidate_id], deviation[candidate_id]
        )
        focus_signals = {
            "semantic_attention": round(attention, 4),
            "declared_location": round(_deixis_support(candidate, by_id), 4),
            "sam_confidence": round(_sam_confidence(candidate, by_id), 4),
            "visual_salience": visual_salience,
            "semantic_consensus": round(consensus, 4),
            "hierarchy_alignment": round(alignment, 4),
            "coverage_presence": coverage[candidate_id],
        }
        ambience_signals = {
            "field_coverage": coverage[candidate_id],
            "coarse_scale": round(_coarse_scale(candidate, by_id), 4),
            "cross_level_persistence": round(_persistence(candidate, by_id), 4),
            "semantic_context": round(_semantic_context(candidate, by_id), 4),
            "hierarchy_alignment": round(alignment, 4),
            "semantic_residual_field": round(_residual_field(candidate, by_id), 4),
        }
        candidate["ranking"] = {
            "focus": {
                "signals": focus_signals,
                "score": round(sum(focus_signals.values()) / len(focus_signals), 4),
            },
            "ambiance": {
                "signals": ambience_signals,
                "score": round(sum(ambience_signals.values()) / len(ambience_signals), 4),
            },
        }
    return selectable


def rank_pairs(candidates: list[dict]) -> list[dict]:
    """Score every ordered focus/ambiance pair jointly; no delta-E cutoff."""
    raw_pairs = []
    for focus in candidates:
        for ambiance in candidates:
            if focus["candidate_id"] == ambiance["candidate_id"]:
                continue
            overlap = _mask_overlap(focus["_mask"], ambiance["_mask"])
            union_coverage = float((focus["_mask"] | ambiance["_mask"]).mean())
            delta_e = math.dist(focus["lab"], ambiance["lab"])
            focus_roles = _candidate_roles(focus)
            ambience_roles = _candidate_roles(ambiance)
            shared_roles = sorted(focus_roles & ambience_roles)
            role_union = focus_roles | ambience_roles
            relation = len(shared_roles) / max(1, len(role_union))
            role_diversity = (
                len(focus_roles ^ ambience_roles) / len(role_union)
                if role_union else 0.5
            )
            bridge = (
                float(focus.get("cross_source", {}).get("best_overlap_iou", 0.0))
                + float(ambiance.get("cross_source", {}).get("best_overlap_iou", 0.0))
            ) / 2
            raw_pairs.append({
                "focus_id": focus["candidate_id"],
                "ambiance_id": ambiance["candidate_id"],
                "_delta_e": delta_e,
                "_union_coverage": union_coverage,
                "_spatial_iou": overlap["iou"],
                "_bridge": bridge,
                "_relation": relation,
                "_role_diversity": role_diversity,
                "shared_semantic_roles": shared_roles,
            })

    delta = _normalise({str(index): pair["_delta_e"] for index, pair in enumerate(raw_pairs)})
    coverage = _normalise({str(index): pair["_union_coverage"] for index, pair in enumerate(raw_pairs)})
    for index, pair in enumerate(raw_pairs):
        focus = next(candidate for candidate in candidates if candidate["candidate_id"] == pair["focus_id"])
        ambiance = next(candidate for candidate in candidates if candidate["candidate_id"] == pair["ambiance_id"])
        individual = (
            focus["ranking"]["focus"]["score"]
            + ambiance["ranking"]["ambiance"]["score"]
        ) / 2
        joint_signals = {
            "spatial_complement": round(1.0 - pair["_spatial_iou"], 4),
            "combined_coverage": coverage[str(index)],
            "perceptual_separation": delta[str(index)],
            "cross_source_bridge": round(pair["_bridge"], 4),
            "role_diversity": round(pair["_role_diversity"], 4),
        }
        pair["metrics"] = {
            "delta_e": round(pair["_delta_e"], 3),
            "spatial_iou": pair["_spatial_iou"],
            "duplicate_mask_redundancy": pair["_spatial_iou"],
            "union_coverage": round(pair["_union_coverage"], 4),
            "same_exact_rgb": focus["rgb"] == ambiance["rgb"],
            "shared_semantic_roles": pair["shared_semantic_roles"],
            "shared_reading_relation": round(pair["_relation"], 4),
            "chromatic_relation": _chromatic_relation(focus["lab"], ambiance["lab"]),
            "focus_source": focus["source"],
            "ambiance_source": ambiance["source"],
        }
        pair["evidence"] = {
            "focus_candidate_score": focus["ranking"]["focus"]["score"],
            "ambiance_candidate_score": ambiance["ranking"]["ambiance"]["score"],
            "joint_signals": joint_signals,
        }
        pair["score"] = round((individual + sum(joint_signals.values()) / len(joint_signals)) / 2, 4)
        for key in ("_delta_e", "_union_coverage", "_spatial_iou", "_bridge", "_relation", "_role_diversity"):
            del pair[key]
    return sorted(raw_pairs, key=lambda pair: (-pair["score"], pair["focus_id"], pair["ambiance_id"]))


def _public_candidate(candidate: dict, role: str | None = None) -> dict:
    result = {key: value for key, value in candidate.items() if not key.startswith("_")}
    if role is not None:
        result = {
            "role": role,
            "candidate_id": candidate["candidate_id"],
            "hex": candidate["hex"],
            "rgb": candidate["rgb"],
            "lab": candidate["lab"],
            "source": candidate["source"],
            "provenance": candidate["provenance"],
            "evidence": candidate["ranking"][role],
            "cross_source": candidate.get("cross_source", {}),
        }
    return result


def reduce_to_two_colours(candidates: list[dict]) -> dict:
    """Select an ordered pair and return an extensible E5 palette schema."""
    selectable = rank_candidates(candidates)
    pairs = rank_pairs(selectable)
    if not pairs:
        raise ValueError("E5 needs at least two measured, selectable candidates")
    by_id = {candidate["candidate_id"]: candidate for candidate in selectable}
    chosen = pairs[0]
    focus = by_id[chosen["focus_id"]]
    ambiance = by_id[chosen["ambiance_id"]]
    return {
        "palette_size": 2,
        "roles": {
            "focus": _public_candidate(focus, "focus"),
            "ambiance": _public_candidate(ambiance, "ambiance"),
        },
        "pair_metrics": chosen["metrics"],
        "pair_evidence": chosen["evidence"],
        "pair_score": chosen["score"],
        "method": {
            "name": "E5 equal-weight evidence baseline",
            "candidate_policy": "measured semantic-mask means plus frozen E4 hierarchy-region means",
            "pair_policy": "ordered joint evaluation; candidate identity is distinct, but no minimum delta-E is imposed",
            "agreement_policy": "mutual_majority means one opposite-source candidate covers at least half of each region",
        },
        "ranked_pairs": pairs,
    }


def public_candidates(candidates: list[dict]) -> list[dict]:
    """Strip in-memory masks before an E5 JSON artifact is written."""
    return [_public_candidate(candidate) for candidate in candidates]