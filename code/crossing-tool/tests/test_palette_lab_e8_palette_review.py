import numpy as np

from scripts.palette_lab import e8_palette_review as REV


def _narratology(stands_out=1, distinctions=1):
    return {
        "what_stands_out": [
            {"entity": f"entity {index}", "against": f"ground {index}",
             "what_makes_it_stand_out": "", "why_it_matters": ""}
            for index in range(stands_out)
        ],
        "strongest_cue": {"visible_cue": "the lit doorway"},
        "organizing_distinctions": [
            {"distinction": f"distinction {index}", "side_a": f"side a {index}",
             "side_b": f"side b {index}"}
            for index in range(distinctions)
        ],
    }


def _carrier(a="the lit doorway", b="the shuttered shopfronts", strength="partial"):
    return {
        "carriers": [{
            "two_colour_strength": strength,
            "carrier_a": {"visible_material": a},
            "carrier_b": {"visible_material": b},
        }]
    }


def _articulation(a="the lit doorway", b="rain-slick asphalt",
                  relationship="broader_field", strength="partial"):
    return {
        "articulations": [{
            "two_colour_strength": strength,
            "chromatic_articulation": {
                "distinguished_material": a,
                "counterfield_material": b,
                "relationship": relationship,
            },
        }]
    }


_PRODUCTION = {"foreground_rgb": [11, 6, 5], "background_rgb": [206, 179, 148]}


def _collect(**kwargs):
    return REV.collect_hypotheses(
        kwargs.get("narratology", _narratology()),
        kwargs.get("carrier", _carrier()),
        kwargs.get("articulation", _articulation()),
        kwargs.get("production", _PRODUCTION),
    )


class TestCollectHypotheses:
    def test_direct_comes_from_the_carrier_pair(self):
        choice = _collect()["1"]
        assert choice["strategy"] == "direct"
        assert choice["source_stage"] == "carrier-v1"
        assert choice["materials"] == ["the lit doorway", "the shuttered shopfronts"]

    def test_field_comes_from_the_articulation_pair(self):
        choice = _collect()["2"]
        assert choice["strategy"] == "field"
        assert choice["source_stage"] == "articulation-v1"
        assert choice["materials"] == ["the lit doorway", "rain-slick asphalt"]

    def test_control_uses_the_frozen_production_palette_without_a_model(self):
        choice = _collect()["4"]
        assert choice["strategy"] == "control"
        assert choice["colours"][0]["hex"] == "#0b0605"
        assert choice["colours"][1]["rgb"] == [206, 179, 148]
        assert "no narratological evidence" in choice["source_note"]

    def test_a_missing_production_palette_leaves_the_control_inactive(self):
        choice = _collect(production=None)["4"]
        assert choice["active"] is False
        assert choice["colours"] == []

    def test_every_choice_is_labelled(self):
        labels = {key: value["label"] for key, value in _collect().items()}
        assert labels["1"] == "DIRECT"
        assert labels["2"] == "FIELD"
        assert labels["4"] == "CONTROL"


class TestAlternativeOrRefusal:
    def test_a_second_organizing_distinction_becomes_the_alternative(self):
        choice = _collect(narratology=_narratology(distinctions=2))["3"]
        assert choice["strategy"] == "alternative"
        assert choice["materials"] == ["side a 1", "side b 1"]

    def test_a_second_stands_out_relation_becomes_the_alternative(self):
        choice = _collect(narratology=_narratology(stands_out=2))["3"]
        assert choice["strategy"] == "alternative"
        assert choice["materials"] == ["entity 1", "ground 1"]

    def test_a_second_distinction_is_preferred_over_a_second_stands_out(self):
        choice = _collect(narratology=_narratology(stands_out=2, distinctions=2))["3"]
        assert choice["materials"] == ["side a 1", "side b 1"]

    def test_a_single_relation_gives_refusal_not_a_manufactured_palette(self):
        choice = _collect()["3"]
        assert choice["strategy"] == "no_adequate_two_colour"
        assert choice["materials"] == []
        assert choice["active"] is True

    def test_refusal_carries_the_frozen_verdicts_it_would_contradict(self):
        choice = _collect(
            carrier=_carrier(strength="strong"),
            articulation=_articulation(strength="strong"),
        )["3"]
        assert choice["frozen_support"]["carrier_v1_strength"] == "strong"
        assert choice["frozen_support"]["articulation_v1_strength"] == "strong"


class TestConvergence:
    def test_identical_material_pairs_collapse(self):
        choices = REV.mark_convergence(_collect(
            articulation=_articulation(a="the lit doorway", b="the shuttered shopfronts")
        ))
        assert choices["2"]["converges_with"] == ["1"]
        assert choices["2"]["active"] is False
        assert choices["1"]["converged_by"] == ["2"]

    def test_wording_differences_that_normalise_away_still_collapse(self):
        choices = REV.mark_convergence(_collect(
            articulation=_articulation(a="The Lit Doorway.",
                                       b="the shuttered shopfronts")
        ))
        assert choices["2"]["converges_with"] == ["1"]

    def test_different_pairs_stay_separate(self):
        choices = REV.mark_convergence(_collect())
        assert choices["2"]["converges_with"] == []
        assert choices["2"]["active"] is True

    def test_convergence_never_uses_a_colour_tolerance(self):
        choices = REV.mark_convergence(_collect())
        assert all("delta" not in str(choice.get("convergence_reason", ""))
                   for choice in choices.values())


class TestOperationalizationPatch:
    def _patch(self, **overrides):
        patch = {
            "source_material": "the sunlit desert floor and distant riders",
            "status": "decomposed",
            "localization_targets": ["sunlit desert floor"],
            "reason": "kept the ground, dropped the riders",
        }
        patch.update(overrides)
        return patch

    def _problems(self, patch, material="the sunlit desert floor and distant riders"):
        return REV.operationalization_problems(patch, material)

    def test_a_well_formed_patch_has_no_problems(self):
        assert self._problems(self._patch()) == []

    def test_unparsed_responses_are_reported(self):
        assert self._problems(None) == ["response did not parse as a JSON object"]

    def test_fields_outside_the_patch_schema_are_rejected(self):
        patch = self._patch()
        patch["uncertainty"] = "restated upstream prose"
        patch["two_colour_strength"] = "partial"
        problems = self._problems(patch)
        assert any("outside the patch schema" in problem for problem in problems)
        assert any("two_colour_strength" in problem for problem in problems)

    def test_the_source_material_must_be_copied(self):
        problems = self._problems(self._patch(source_material="something else"))
        assert any("source_material was not copied" in problem for problem in problems)

    def test_the_status_vocabulary_is_enforced(self):
        problems = self._problems(self._patch(status="simplified"))
        assert any("is not one of" in problem for problem in problems)

    def test_every_allowed_status_parses(self):
        for status in ("atomic", "decomposed", "distributed"):
            assert self._problems(self._patch(status=status)) == [], status

    def test_unusable_must_not_name_targets(self):
        problems = self._problems(self._patch(status="unusable"))
        assert any("'unusable' but targets were named" in problem
                   for problem in problems)

    def test_unusable_with_no_targets_is_accepted(self):
        assert self._problems(
            self._patch(status="unusable", localization_targets=[])
        ) == []

    def test_a_usable_status_needs_targets(self):
        problems = self._problems(self._patch(localization_targets=[]))
        assert any("no localization targets" in problem for problem in problems)

    def test_too_many_targets_are_reported(self):
        problems = self._problems(self._patch(
            localization_targets=["a", "b", "c", "d"]))
        assert any("exceeds the maximum" in problem for problem in problems)

    def test_an_empty_reason_is_reported(self):
        problems = self._problems(self._patch(reason="  "))
        assert any("reason is empty" in problem for problem in problems)


class TestLocalizationTargets:
    def test_targets_are_deduplicated_and_capped(self):
        targets = REV.localization_targets({
            "localization_targets": ["Snow", "snow", "the treeline", "a wall", "sky"]
        })
        assert targets == ["Snow", "the treeline", "a wall"]

    def test_an_unparsed_patch_yields_nothing(self):
        assert REV.localization_targets(None) == []


class TestSegmentationStatus:
    def _result(self, masks, coverage=0.2, error=False):
        union = np.zeros((10, 10), dtype=bool)
        filled = int(round(coverage * union.size))
        union.flat[:filled] = True
        return {
            "masks": masks,
            "union": union,
            "attempts": [{"error": "boom" if error else None}],
        }

    def test_one_clean_mask_is_a_success(self):
        assert REV.segmentation_status(self._result([{"mask": None}])) == "success"

    def test_no_masks_is_reported_not_hidden(self):
        assert REV.segmentation_status(self._result([])) == "no_mask"

    def test_an_exception_is_distinguished_from_an_empty_result(self):
        assert REV.segmentation_status(self._result([], error=True)) == "error"

    def test_several_instances_are_flagged(self):
        result = self._result([{"mask": None}, {"mask": None}])
        assert REV.segmentation_status(result) == "multiple_masks"

    def test_a_mask_that_swallows_the_frame_is_flagged(self):
        result = self._result([{"mask": None}], coverage=0.95)
        assert REV.segmentation_status(result) == "suspicious_broad"

    def test_the_broad_threshold_is_a_single_global_constant(self):
        assert REV.BROAD_COVERAGE == 0.90


class TestWorstStatus:
    def test_the_worst_target_decides_the_material(self):
        assert REV.worst_status(["success", "no_mask"]) == "no_mask"
        assert REV.worst_status(["success", "multiple_masks"]) == "multiple_masks"
        assert REV.worst_status(["error", "success"]) == "error"

    def test_all_successful_stays_successful(self):
        assert REV.worst_status(["success", "success"]) == "success"


class TestMeasurement:
    def test_the_colour_is_the_plain_mean_of_the_masked_pixels(self):
        arr = np.zeros((4, 4, 3), dtype=np.uint8)
        arr[:2] = (200, 100, 50)
        arr[2:] = (0, 0, 0)
        mask = np.zeros((4, 4), dtype=bool)
        mask[:2] = True
        assert REV.measure(arr, mask)["rgb"] == [200, 100, 50]

    def test_a_large_dark_area_does_not_outweigh_a_small_bright_one(self):
        arr = np.zeros((4, 4, 3), dtype=np.uint8)
        arr[0, 0] = (255, 255, 255)
        mask = np.zeros((4, 4), dtype=bool)
        mask[0, 0] = True
        assert REV.measure(arr, mask)["rgb"] == [255, 255, 255]

    def test_colour_records_carry_hex_and_lab(self):
        record = REV._colour_record([206, 179, 148])
        assert record["hex"] == "#ceb394"
        assert len(record["lab"]) == 3

    def test_no_colour_is_invented_for_a_missing_value(self):
        assert REV._colour_record(None) is None


class TestReviewTemplate:
    def test_the_template_is_blank_and_lists_only_selectable_choices(self):
        choices = REV.mark_convergence(_collect(
            articulation=_articulation(a="the lit doorway",
                                       b="the shuttered shopfronts")
        ))
        template = REV.review_template("PAL-015", choices, "v1")
        assert template["choice"] is None
        assert template["reviewer"] is None
        assert template["selectable"] == ["1", "3", "4"]
        assert template["experiment_version"] == "measured-review-v1"


class TestNormalisation:
    def test_case_punctuation_and_spacing_are_ignored(self):
        assert REV.normalise_material("The Red Coat.") == "the red coat"
        assert REV.normalise_material("  snow-covered   ground ") == "snow covered ground"
