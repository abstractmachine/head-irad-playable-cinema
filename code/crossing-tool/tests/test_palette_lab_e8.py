from scripts.palette_lab import e8_curator as E8


def _proposition(identifier, side_a, side_b, **overrides):
    proposition = {
        "id": identifier,
        "side_a": side_a,
        "side_b": side_b,
        "relationship": f"{side_a} held against {side_b}",
        "why_it_organizes_the_frame": "it structures the frame",
        "formal_basis": "tonal opposition",
        "semantic_basis": "an agent against a setting",
        "spatial_basis": "one is contained by the other",
        "figure_side": "side_a",
        "role_reasoning": f"{side_a} reads as figure",
        "shares_regions_with": [],
        "uncertainty": f"unsure whether {side_b} is passive",
    }
    proposition.update(overrides)
    return proposition


def _parsed(*propositions, doing="the frame stages an encounter"):
    return {"what_the_frame_is_doing": doing, "propositions": list(propositions)}


def _four():
    return [
        _proposition("P1", "the man in the red coat", "the snowy forest"),
        _proposition("P2", "the bright doorway", "the dark interior"),
        _proposition("P3", "the distant riders", "the desert plain"),
        _proposition("P4", "the silhouette", "the saturated field"),
    ]


class TestShapeV2:
    def test_a_well_formed_response_has_no_problems(self):
        assert E8._shape_problems(_parsed(*_four()), "v2") == []

    def test_unparsed_and_empty_responses_are_reported(self):
        assert E8._shape_problems(None, "v2") == ["response did not parse as a JSON object"]
        assert E8._shape_problems({"propositions": []}, "v2") == ["no propositions array"]

    def test_proposition_count_outside_the_requested_range_is_reported(self):
        problems = E8._shape_problems(_parsed(*_four()[:2]), "v2")
        assert any("expected 4-6" in problem for problem in problems)

    def test_missing_prose_fields_are_named(self):
        propositions = _four()
        propositions[0]["uncertainty"] = "  "
        problems = E8._shape_problems(_parsed(*propositions), "v2")
        assert any("missing uncertainty" in problem for problem in problems)

    def test_numbers_in_the_evidence_fields_are_flagged(self):
        propositions = _four()
        propositions[1]["formal_basis"] = "covers 40 percent of the frame"
        problems = E8._shape_problems(_parsed(*propositions), "v2")
        assert any("formal_basis contains a number" in problem for problem in problems)

    def test_empty_frame_statement_is_reported(self):
        problems = E8._shape_problems(_parsed(*_four(), doing=""), "v2")
        assert any("what_the_frame_is_doing is empty" in problem for problem in problems)


class TestOrientationClaim:
    def test_figure_side_must_be_one_of_the_three_allowed_values(self):
        propositions = _four()
        propositions[0]["figure_side"] = "the cave interior functions as figure"
        problems = E8._shape_problems(_parsed(*propositions), "v2")
        assert any("figure_side" in problem and "not one of" in problem for problem in problems)

    def test_declining_to_claim_a_role_is_valid(self):
        propositions = _four()
        propositions[0]["figure_side"] = "no_role_claim"
        assert E8._shape_problems(_parsed(*propositions), "v2") == []

    def test_v1_records_are_checked_against_the_v1_field_set(self):
        propositions = _four()
        for proposition in propositions:
            proposition.pop("figure_side")
            proposition.pop("role_reasoning")
            proposition["role_claim"] = "side A functions as figure"
        assert E8._shape_problems(_parsed(*propositions), "v1") == []
        assert E8._shape_problems(_parsed(*propositions), "v2")

    def test_versions_after_v1_all_use_the_role_enum_schema(self):
        parsed = _parsed(*_four())
        for version in ("v2", "v3", "v4"):
            assert E8._shape_problems(parsed, version) == []
        assert E8._uses_role_enum("v1") is False


class TestSharedRegionDeclaration:
    def test_declaring_an_overlap_with_an_earlier_reading_is_valid(self):
        propositions = _four()
        propositions[3]["shares_regions_with"] = ["P1"]
        assert E8._shape_problems(_parsed(*propositions), "v2") == []

    def test_naming_an_unknown_or_later_reading_is_reported(self):
        propositions = _four()
        propositions[0]["shares_regions_with"] = ["P3"]
        problems = E8._shape_problems(_parsed(*propositions), "v2")
        assert any("not earlier proposition ids" in problem for problem in problems)

    def test_a_non_list_declaration_is_reported(self):
        propositions = _four()
        propositions[0]["shares_regions_with"] = "P1"
        problems = E8._shape_problems(_parsed(*propositions), "v2")
        assert any("is not a list" in problem for problem in problems)


class TestDiversity:
    def test_distinct_readings_report_low_overlap(self):
        diversity = E8._diversity(_four())
        assert diversity["propositions"] == 4
        assert diversity["distinct_side_pairs"] == 4
        assert diversity["pairs_above_half_overlap"] == []

    def test_paraphrases_of_one_relation_are_surfaced(self):
        propositions = [
            _proposition("P1", "the dark cave interior", "the sunlit desert expanse"),
            _proposition("P2", "the sunlit desert expanse", "the dark cave interior"),
        ]
        diversity = E8._diversity(propositions)
        assert diversity["highest_term_overlap"]["term_overlap"] == 1.0
        assert len(diversity["pairs_above_half_overlap"]) == 1

    def test_declared_overlaps_are_counted(self):
        propositions = _four()
        propositions[1]["shares_regions_with"] = ["P1"]
        propositions[2]["shares_regions_with"] = ["P1"]
        assert E8._diversity(propositions)["declared_shared_regions"] == 2

    def test_verbatim_reuse_across_readings_is_detected(self):
        propositions = _four()
        propositions[1]["uncertainty"] = propositions[0]["uncertainty"]
        propositions[1]["role_reasoning"] = propositions[0]["role_reasoning"]
        repeated = E8._diversity(propositions)["repeated_field_text"]
        assert repeated["uncertainty"] == 1
        assert repeated["role_reasoning"] == 1

    def test_terms_ignore_short_words(self):
        assert E8._terms({"side_a": "the man in a red coat", "side_b": ""}) == {"coat"}
