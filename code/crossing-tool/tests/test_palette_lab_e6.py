import numpy as np

from scripts.palette_lab import e6_review as E6


def _mask(size, x0, x1, y0, y1):
    mask = np.zeros((size, size), dtype=bool)
    mask[y0:y1, x0:x1] = True
    return mask


def _candidate(candidate_id, source, rgb, mask, *, role=None, phrase="",
               level=None, region=0, interpretation=""):
    candidate = {
        "candidate_id": candidate_id,
        "source": source,
        "available": True,
        "rgb": list(rgb),
        "hex": "#{:02x}{:02x}{:02x}".format(*rgb),
        "lab": [50.0, 10.0, -5.0],
        "area": int(mask.sum()),
        "coverage": round(float(mask.mean()), 5),
        "centroid": [0.2, 0.8],
        "bbox": [0, 0, 1, 1],
        "provenance": {
            "source": source,
            "interpretation": interpretation,
            "declared_location": "center",
            "declared_extent": "large",
        },
        "_mask": mask,
    }
    if source == "semantic":
        candidate["semantic"] = {"role": role, "segment_phrase": phrase, "mask_index": 0}
    else:
        candidate["hierarchy"] = {"level": level, "region_identity": region}
    return candidate


def _pool():
    return [
        _candidate("semantic:chromatic:red coat:0", "semantic", [126, 9, 4],
                   _mask(8, 0, 2, 0, 2), role="chromatic", phrase="red coat",
                   interpretation="a red coat against snow"),
        _candidate("semantic:compositional:snowy ground:0", "semantic", [199, 198, 204],
                   _mask(8, 0, 8, 4, 8), role="compositional", phrase="snowy ground"),
        _candidate("hierarchy:L3:R0", "hierarchy", [42, 46, 48],
                   _mask(8, 0, 8, 0, 4), level=3, region=0),
    ]


def _records():
    return E6.build_review_records(_pool())


def _e5_reduction(focus_id, ambiance_id):
    return {"roles": {"focus": {"candidate_id": focus_id},
                      "ambiance": {"candidate_id": ambiance_id}}}


def _e5_ranking(pool, selected_focus, selected_ambiance):
    pairs = []
    for focus in pool:
        for ambiance in pool:
            if focus["candidate_id"] == ambiance["candidate_id"]:
                continue
            selected = (focus["candidate_id"] == selected_focus
                        and ambiance["candidate_id"] == selected_ambiance)
            pairs.append({
                "focus_id": focus["candidate_id"],
                "ambiance_id": ambiance["candidate_id"],
                "score": 0.9 if selected else 0.4,
            })
    pairs.sort(key=lambda pair: -pair["score"])
    return {
        "candidate_ranking": [
            {
                "candidate_id": candidate["candidate_id"],
                "source": candidate["source"],
                "focus": {"score": 0.9 if candidate["candidate_id"] == selected_focus else 0.3},
                "ambiance": {"score": 0.9 if candidate["candidate_id"] == selected_ambiance else 0.3},
            }
            for candidate in pool
        ],
        "ranked_ordered_pairs": pairs,
    }


def _selection(focus_review_id, ambiance_review_id, **overrides):
    selection = {
        "id": "PAL-015",
        "phase": "diagnostic",
        "reviewer": "A",
        "reviewed": True,
        "focus": {"review_id": focus_review_id, "candidate_id": ""},
        "ambiance": {"review_id": ambiance_review_id, "candidate_id": ""},
        "pair_assessment": "strong",
        "rationale": "the coat is the event",
        "candidate_pool": {"sufficiency": "sufficient", "reasons": [], "notes": ""},
        "notes": "",
    }
    selection.update(overrides)
    return selection


class TestReviewRecords:
    def test_records_are_numbered_in_inventory_order_not_score_order(self):
        records = _records()
        assert [record["review_id"] for record in records] == ["C01", "C02", "C03"]
        assert records[0]["candidate_id"] == "semantic:chromatic:red coat:0"

    def test_semantic_and_hierarchy_records_expose_reviewer_facing_fields(self):
        semantic, _, hierarchy = _records()
        assert semantic["label"] == "red coat"
        assert semantic["semantic_role"] == "chromatic"
        assert semantic["interpretation"] == "a red coat against snow"
        assert semantic["hierarchy_level"] is None
        assert hierarchy["label"] == "level 3 region 0"
        assert hierarchy["hierarchy_level"] == 3
        assert hierarchy["semantic_role"] is None

    def test_records_carry_no_e5_score_or_rank(self):
        for record in _records():
            assert "ranking" not in record
            assert "score" not in record
            assert "rank" not in record

    def test_observed_cell_uses_the_e4_vocabulary(self):
        assert E6.observed_cell([0.2, 0.8]) == "lower_left"
        assert E6.observed_cell([0.5, 0.5]) == "center"
        assert E6.observed_cell([0.99, 0.01]) == "upper_right"
        assert E6.observed_cell(None) == ""


class TestPoolVerification:
    def test_identical_rebuild_is_reported_as_identical(self):
        pool = _pool()
        frozen = {"measured_candidates": [
            {"candidate_id": candidate["candidate_id"], "rgb": candidate["rgb"],
             "hex": candidate["hex"], "lab": candidate["lab"], "area": candidate["area"]}
            for candidate in pool
        ]}
        report = E6.verify_against_frozen_e5(pool, frozen)
        assert report["identical_pool"] is True
        assert report["matched"] == 3
        assert report["measurement_drift"] == []

    def test_drift_is_reported_rather_than_corrected(self):
        pool = _pool()
        frozen = {"measured_candidates": [
            {"candidate_id": candidate["candidate_id"], "rgb": [0, 0, 0], "hex": "#000000",
             "lab": [0.0, 0.0, 0.0], "area": candidate["area"]}
            for candidate in pool
        ]}
        report = E6.verify_against_frozen_e5(pool, frozen)
        assert report["identical_pool"] is False
        assert len(report["measurement_drift"]) == 3
        assert report["measurement_drift"][0]["delta_e"] > 0

    def test_missing_and_extra_candidates_are_kept_separate(self):
        pool = _pool()
        frozen = {"measured_candidates": [
            {"candidate_id": "semantic:chromatic:red coat:0", "rgb": [126, 9, 4],
             "hex": "#7e0904", "lab": [50.0, 10.0, -5.0], "area": pool[0]["area"]},
            {"candidate_id": "semantic:gone:phrase:0", "rgb": [1, 1, 1],
             "hex": "#010101", "lab": [0.0, 0.0, 0.0], "area": 5},
        ]}
        report = E6.verify_against_frozen_e5(pool, frozen)
        assert report["missing_from_rebuild"] == ["semantic:gone:phrase:0"]
        assert report["absent_from_e5"] == [
            "hierarchy:L3:R0", "semantic:compositional:snowy ground:0"
        ]


class TestComparison:
    def test_exact_pair_agreement(self):
        pool = _pool()
        records = _records()
        comparison = E6.compare_one(
            _selection("C02", "C03"), records,
            _e5_reduction("semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
            _e5_ranking(pool, "semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
        )
        assert comparison["agreement"]["pair_exact"] is True
        assert comparison["classification"] == "exact_pair_agreement"

    def test_human_choice_present_in_pool_but_unselected_by_e5(self):
        pool = _pool()
        comparison = E6.compare_one(
            _selection("C01", "C03"), _records(),
            _e5_reduction("semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
            _e5_ranking(pool, "semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
        )
        assert comparison["agreement"]["focus_exact"] is False
        assert comparison["agreement"]["ambiance_exact"] is True
        assert comparison["classification"] == "partial_agreement_present_but_unselected"
        assert comparison["e5_view_of_human_choice"]["pair"]["rank"] > 1
        assert comparison["e5_view_of_human_choice"]["selected_pair"]["rank"] == 1

    def test_insufficient_pool_is_never_merged_with_misselection(self):
        pool = _pool()
        selection = _selection("C01", "C03", candidate_pool={
            "sufficiency": "insufficient",
            "reasons": ["small_accent_merged_away"],
            "notes": "the accent only exists relationally",
        })
        comparison = E6.compare_one(
            selection, _records(),
            _e5_reduction("semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
            _e5_ranking(pool, "semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
        )
        assert comparison["classification"] == "pool_insufficient"
        assert comparison["human"]["focus"]["review_id"] == "C01"
        assert comparison["candidate_pool"]["reasons"] == ["small_accent_merged_away"]

    def test_role_swap_is_detected_and_measurements_are_raw(self):
        pool = _pool()
        masks = {candidate["candidate_id"]: candidate["_mask"] for candidate in pool}
        comparison = E6.compare_one(
            _selection("C03", "C02"), _records(),
            _e5_reduction("semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
            _e5_ranking(pool, "semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
            masks,
        )
        assert comparison["agreement"]["role_swap"] is True
        assert comparison["agreement"]["focus_measurement"]["mask_iou"] == 0.0
        assert comparison["equivalence_rule"] is None

    def test_unresolved_selection_is_reported_not_guessed(self):
        pool = _pool()
        comparison = E6.compare_one(
            _selection("C99", "C02"), _records(),
            _e5_reduction("semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
            _e5_ranking(pool, "semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
        )
        assert comparison["classification"] == "unresolved_selection"
        assert comparison["unresolved_roles"] == ["focus"]
        assert comparison["agreement"] is None


class TestSummary:
    def _comparisons(self, *reviewers):
        pool = _pool()
        reduction = _e5_reduction(
            "semantic:compositional:snowy ground:0", "hierarchy:L3:R0")
        ranking = _e5_ranking(
            pool, "semantic:compositional:snowy ground:0", "hierarchy:L3:R0")
        return [
            E6.compare_one(
                _selection(focus, ambiance, reviewer=reviewer), _records(),
                reduction, ranking,
            )
            for reviewer, focus, ambiance in reviewers
        ]

    def test_single_reviewer_is_recorded_explicitly(self):
        summary = E6.summarise(self._comparisons(("A", "C01", "C03")))
        assert summary["reviewer_count"] == 1
        assert summary["single_reviewer"] is True
        assert summary["single_reviewer_note"]

    def test_reviewer_disagreement_is_preserved_not_collapsed(self):
        summary = E6.summarise(self._comparisons(
            ("A", "C01", "C03"), ("B", "C01", "C02")))
        disagreement = summary["reviewer_disagreement"]["PAL-015"]
        assert disagreement["focus_unanimous"] is True
        assert disagreement["ambiance_unanimous"] is False
        assert disagreement["ambiance_choices"] == {
            "A": "hierarchy:L3:R0",
            "B": "semantic:compositional:snowy ground:0",
        }
        assert summary["per_reviewer"]["A"]["reviewed_images"] == 1
        assert summary["per_reviewer"]["B"]["reviewed_images"] == 1


class TestCensus:
    def test_census_locates_every_candidate_in_the_frozen_e5_ranking(self):
        pool = _pool()
        rows = E6.census(
            _records(),
            _e5_ranking(pool, "semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
            _e5_reduction("semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
        )
        assert [row["review_id"] for row in rows] == ["C01", "C02", "C03"]
        by_id = {row["review_id"]: row for row in rows}
        assert by_id["C02"]["e5_selected_as"] == "focus"
        assert by_id["C03"]["e5_selected_as"] == "ambiance"
        assert by_id["C01"]["e5_selected_as"] is None
        assert by_id["C02"]["best_pair_rank_as_focus"] == 1
        assert by_id["C01"]["e5_focus"]["rank"] > by_id["C02"]["e5_focus"]["rank"]

    def test_census_carries_the_area_scale_an_accent_argument_needs(self):
        pool = _pool()
        rows = E6.census(
            _records(),
            _e5_ranking(pool, "semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
            _e5_reduction("semantic:compositional:snowy ground:0", "hierarchy:L3:R0"),
        )
        accent = next(row for row in rows if row["label"] == "red coat")
        assert accent["area_percent"] == 6.25
        assert accent["semantic_role"] == "chromatic"
        assert accent["best_pair_rank_as_focus"] is not None


class TestProtocolConstants:
    def test_diagnostic_five_are_reviewed_first(self):
        order = E6.review_order()
        assert order[:5] == ["PAL-001", "PAL-004", "PAL-015", "PAL-022", "PAL-019"]
        assert sorted(order) == sorted(set(order))
        assert len(order) == 9

    def test_template_offers_choices_without_prefilling_an_answer(self):
        template = E6.selection_template("PAL-015", _records(), "diagnostic")
        assert template["focus"] == {"review_id": "", "candidate_id": ""}
        assert template["reviewed"] is False
        assert template["_choices"]["review_ids"] == ["C01", "C02", "C03"]
        assert template["_choices"]["pair_assessment"] == list(E6.PAIR_ASSESSMENTS)
