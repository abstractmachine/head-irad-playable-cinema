from pathlib import Path

import numpy as np
from PIL import Image

from scripts.palette_lab import two_color as E5


def _candidate(candidate_id, source, rgb, mask, *, role=None, level=None,
               extent="", distance=None, residual=0.0):
    rgb = list(rgb)
    lab = E5._rgb_to_lab(np.asarray([rgb], dtype=np.uint8))[0]
    candidate = {
        "candidate_id": candidate_id,
        "source": source,
        "available": True,
        "rgb": rgb,
        "hex": E5.rgb_hex(rgb),
        "lab": [round(float(value), 1) for value in lab],
        "lightness": round(float(lab[0]), 1),
        "chroma": round(float(np.hypot(lab[1], lab[2])), 1),
        "area": int(mask.sum()),
        "coverage": round(float(mask.mean()), 5),
        "centroid": [0.5, 0.5],
        "bbox": [0, 0, mask.shape[1] - 1, mask.shape[0] - 1],
        "provenance": {"source": source},
        "semantic_roles": [role] if role else [],
        "_mask": mask,
    }
    if source == "semantic":
        candidate["semantic"] = {
            "role": role,
            "segment_phrase": role,
            "mask_index": 0,
            "distance_to_declared": distance,
            "declared_extent": extent,
        }
    else:
        candidate["hierarchy"] = {
            "level": level,
            "region_identity": 0,
            "residual_fraction": residual,
        }
    return candidate


def test_recover_hierarchy_masks_uses_exact_e4_region_colours(tmp_path):
    pixels = np.zeros((4, 4, 3), dtype=np.uint8)
    pixels[:2] = (12, 34, 56)
    pixels[2:] = (200, 180, 160)
    Image.fromarray(pixels, "RGB").save(tmp_path / "04-hierarchy-03.png")
    hierarchy = {
        "3": {"regions": [
            {"index": 0, "rgb": [12, 34, 56]},
            {"index": 1, "rgb": [200, 180, 160]},
        ]}
    }

    recovered, report = E5.recover_hierarchy_masks(
        hierarchy, tmp_path, working_size=4
    )

    assert recovered[3][0].sum() == 8
    assert recovered[3][1].sum() == 8
    assert not np.any(recovered[3][0] & recovered[3][1])
    assert report["levels"]["3"]["unmapped_snapshot_pixels"] == 0


def test_semantic_candidates_preserve_e4_readings_unavailable_on_replay():
    inventory = {
        "semantic_roles": {
            "chromatic": {
                "interpretation": "red signal",
                "color_language": "red",
                "location": "upper_left",
                "extent": "small",
                "mask_count": 1,
                "masks": [{
                    "phrase": "red signal", "mask_index": 0, "area": 4,
                    "centroid": [0.125, 0.125], "bbox": [0, 0, 1, 1],
                    "score": 0.9, "distance_to_declared": 0.0,
                    "inside_declared_cell": True,
                }],
            },
            "graphic": {
                "interpretation": "unspatialized caption",
                "color_language": "white type",
                "location": "center",
                "extent": "small",
                "mask_count": 0,
                "segment_phrases": ["caption"],
                "masks": [],
            },
        }
    }
    arr = np.full((4, 4, 3), (20, 30, 40), dtype=np.uint8)
    mask = np.zeros((4, 4), dtype=bool)
    mask[:2, :2] = True
    arr[mask] = (220, 20, 10)

    candidates, unavailable = E5.semantic_candidates(
        inventory,
        {"red signal": [{"mask_index": 0, "mask": mask, "score": 0.95}]},
        arr,
    )

    assert len(candidates) == 1
    assert candidates[0]["rgb"] == [220, 20, 10]
    assert candidates[0]["semantic"]["replay"]["area_delta_pixels"] == 0
    assert {item["candidate_id"] for item in unavailable} == {
        "semantic-reading:graphic"
    }


def test_two_colour_reduction_returns_ordered_roles_without_delta_e_gate():
    focus_mask = np.zeros((4, 4), dtype=bool)
    focus_mask[:2, :2] = True
    field_mask = ~focus_mask
    candidates = [
        _candidate(
            "semantic:chromatic:red", "semantic", (180, 20, 20), focus_mask,
            role="chromatic", extent="small", distance=0.0,
        ),
        _candidate(
            "hierarchy:L3:field", "hierarchy", (181, 21, 20), field_mask,
            level=3, residual=0.9,
        ),
        _candidate(
            "hierarchy:L12:accent", "hierarchy", (180, 20, 20), focus_mask,
            level=12,
        ),
    ]
    E5.add_cross_source_evidence(candidates)
    E5.add_hierarchy_persistence(candidates)

    result = E5.reduce_to_two_colours(candidates)

    assert result["palette_size"] == 2
    assert set(result["roles"]) == {"focus", "ambiance"}
    assert result["roles"]["focus"]["candidate_id"] != result["roles"]["ambiance"]["candidate_id"]
    assert result["ranked_pairs"]
    assert any(pair["metrics"]["delta_e"] < 2 for pair in result["ranked_pairs"])
    assert result["method"]["pair_policy"].endswith("no minimum delta-E is imposed")


def test_chromatic_relation_reports_neutral_limitation_and_hue_opposition():
    neutral = E5._chromatic_relation([50.0, 1.0, 1.0], [50.0, 20.0, 20.0])
    opposed = E5._chromatic_relation([50.0, 30.0, 0.0], [50.0, -30.0, 0.0])

    assert neutral["available"] is False
    assert "near-neutral" in neutral["reason"]
    assert opposed == {
        "available": True,
        "focus_hue_degrees": 0.0,
        "ambiance_hue_degrees": 180.0,
        "hue_difference_degrees": 180.0,
        "opposition": 1.0,
    }


def test_visual_salience_respects_the_frozen_semantic_lens():
    mask = np.ones((2, 2), dtype=bool)
    chromatic = _candidate(
        "semantic:chromatic:neutral", "semantic", (200, 200, 200), mask,
        role="chromatic",
    )
    luminance = _candidate(
        "semantic:luminance:red", "semantic", (200, 20, 20), mask,
        role="luminance",
    )
    hierarchy = _candidate(
        "hierarchy:L3:neutral", "hierarchy", (200, 200, 200), mask, level=3,
    )

    assert E5._visual_salience(chromatic, 0.1, 0.9) == 0.1
    assert E5._visual_salience(luminance, 0.9, 0.1) == 0.1
    assert E5._visual_salience(hierarchy, 0.1, 0.9) == 0.9