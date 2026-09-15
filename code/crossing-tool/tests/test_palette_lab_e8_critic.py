from scripts.palette_lab import e8_critic as CRITIC


def _curator():
    return {
        "what_the_frame_is_doing": "the frame stages an encounter",
        "propositions": [
            {
                "id": "P1", "side_a": "the man in the red coat",
                "side_b": "the snowy forest", "relationship": "warm against cold",
                "why_it_organizes_the_frame": "it carries the whole frame",
                "formal_basis": "chromatic opposition",
                "semantic_basis": "an agent against a setting",
                "spatial_basis": "he is enclosed by it",
                "figure_side": "side_a", "role_reasoning": "he acts",
                "shares_regions_with": [], "uncertainty": "unsure about the trees",
            },
            {
                "id": "P2", "side_a": "the red coat", "side_b": "the snow",
                "relationship": "accent against field",
                "why_it_organizes_the_frame": "the accent anchors the eye",
                "formal_basis": "saturation against neutrality",
                "semantic_basis": "clothing against weather",
                "spatial_basis": "small against large",
                "figure_side": "side_a", "role_reasoning": "the accent leads",
                "shares_regions_with": ["P1"], "uncertainty": "may be the same as P1",
            },
            {
                "id": "P3", "side_a": "the horse", "side_b": "the treeline",
                "relationship": "animal against wood",
                "why_it_organizes_the_frame": "it marks the depth",
                "formal_basis": "dark against dark",
                "semantic_basis": "transport against wilderness",
                "spatial_basis": "beside rather than within",
                "figure_side": "no_role_claim", "role_reasoning": "neither leads",
                "shares_regions_with": [], "uncertainty": "weak formally",
            },
        ],
    }


def _relation(identifier, sources, **overrides):
    relation = {
        "id": identifier,
        "classification": "primary",
        "from_propositions": list(sources),
        "side_a": "the man in the red coat",
        "side_b": "the snowy forest",
        "relationship": "a warm agent against a cold field",
        "why_image_organizing": "it is the frame's principal opposition",
        "would_survive_two_colour_reduction": "the opposition would remain legible",
        "figure_side": "side_a",
        "orientation_reasoning": "the man acts; the field receives",
        "supporting_arguments": [
            {"from_proposition": source, "argument": f"argument from {source}"}
            for source in sources
        ],
        "uncertainty": "the treeline may belong to neither side",
        "alternatives": "the horse could anchor a different reading",
    }
    relation.update(overrides)
    return relation


def _critic(relations, demoted):
    return {
        "reading_of_the_set": "the proposals converge on one opposition",
        "relations": relations,
        "demoted": demoted,
    }


def _demoted(identifier, disposition="rejected", reason="it does not organize the frame"):
    return {"from_proposition": identifier, "disposition": disposition, "reason": reason}


def _well_formed():
    return _critic([_relation("R1", ["P1", "P2"])], [_demoted("P3")])


IDS = ["P1", "P2", "P3"]


class TestRenderPropositions:
    def test_the_other_curators_argument_is_passed_through_in_full(self):
        text = CRITIC.render_propositions(_curator())
        assert "the frame stages an encounter" in text
        assert "[P1]" in text and "[P3]" in text
        assert "chromatic opposition" in text
        assert "role reasoning: he acts" in text

    def test_stage_a_self_declared_overlap_is_shown(self):
        text = CRITIC.render_propositions(_curator())
        assert "shares regions with: P1" in text
        assert "shares regions with: none declared" in text


class TestShape:
    def test_a_well_formed_critique_has_no_problems(self):
        assert CRITIC.shape_problems(_well_formed(), IDS, "v2") == []

    def test_unparsed_and_empty_responses_are_reported(self):
        assert CRITIC.shape_problems(None, IDS, "v2") == ["response did not parse as a JSON object"]
        assert CRITIC.shape_problems({"relations": []}, IDS, "v2") == ["no relations array"]

    def test_missing_prose_is_named(self):
        relation = _relation("R1", ["P1", "P2"], uncertainty="  ")
        problems = CRITIC.shape_problems(_critic([relation], [_demoted("P3")]), IDS, "v2")
        assert any("missing uncertainty" in problem for problem in problems)

    def test_figure_side_must_be_one_of_the_three_allowed_values(self):
        relation = _relation("R1", ["P1", "P2"], figure_side="the man is the figure")
        problems = CRITIC.shape_problems(_critic([relation], [_demoted("P3")]), IDS, "v2")
        assert any("figure_side" in problem and "not one of" in problem for problem in problems)

    def test_unresolved_orientation_is_valid(self):
        relation = _relation("R1", ["P1", "P2"], figure_side="unresolved")
        assert CRITIC.shape_problems(_critic([relation], [_demoted("P3")]), IDS, "v2") == []

    def test_proposition_ids_in_prose_are_not_mistaken_for_numbers(self):
        relation = _relation(
            "R1", ["P1", "P2"],
            why_image_organizing="P1 and P2 both reach for this opposition",
        )
        assert CRITIC.shape_problems(_critic([relation], [_demoted("P3")]), IDS, "v2") == []

    def test_relation_ids_in_prose_are_not_mistaken_for_numbers(self):
        relation = _relation(
            "R2", ["P1", "P2"],
            why_image_organizing="this is a distinct relation from R1",
        )
        assert CRITIC.shape_problems(_critic([relation], [_demoted("P3")]), IDS, "v2") == []

    def test_a_real_number_in_prose_is_reported(self):
        relation = _relation("R1", ["P1", "P2"], relationship="covers 40 percent")
        problems = CRITIC.shape_problems(_critic([relation], [_demoted("P3")]), IDS, "v2")
        assert any("contains a number" in problem for problem in problems)


class TestAccounting:
    def test_every_proposition_must_be_accounted_for(self):
        problems = CRITIC.shape_problems(_critic([_relation("R1", ["P1", "P2"])], []), IDS, "v2")
        assert any("never accounted for: ['P3']" in problem for problem in problems)

    def test_double_accounting_is_reported(self):
        critique = _critic([_relation("R1", ["P1", "P2"])],
                           [_demoted("P1", "demoted"), _demoted("P3")])
        problems = CRITIC.shape_problems(critique, IDS, "v2")
        assert any("more than once: ['P1']" in problem for problem in problems)

    def test_unknown_proposition_ids_are_reported(self):
        critique = _critic([_relation("R1", ["P1", "P9"])], [_demoted("P2"), _demoted("P3")])
        problems = CRITIC.shape_problems(critique, IDS, "v2")
        assert any("unknown propositions ['P9']" in problem for problem in problems)

    def test_a_relation_must_cite_its_sources(self):
        relation = _relation("R1", [], supporting_arguments=[{"from_proposition": "P1", "argument": "x"}])
        problems = CRITIC.shape_problems(_critic([relation], [_demoted("P3")]), IDS, "v2")
        assert any("no from_propositions" in problem for problem in problems)

    def test_consolidating_without_preserving_a_distinct_argument_is_reported(self):
        relation = _relation(
            "R1", ["P1", "P2"],
            supporting_arguments=[{"from_proposition": "P1", "argument": "only one"}],
        )
        problems = CRITIC.shape_problems(_critic([relation], [_demoted("P3")]), IDS, "v2")
        assert any("without preserving" in problem for problem in problems)

    def test_demotions_need_a_known_disposition_and_a_reason(self):
        critique = _critic([_relation("R1", ["P1", "P2"])],
                           [{"from_proposition": "P3", "disposition": "binned", "reason": ""}])
        problems = CRITIC.shape_problems(critique, IDS, "v2")
        assert any("is not one of" in problem for problem in problems)
        assert any("has no reason" in problem for problem in problems)


class TestClassification:
    def test_v2_requires_primary_or_alternative(self):
        relation = _relation("R1", ["P1", "P2"], classification="strongest")
        problems = CRITIC.shape_problems(_critic([relation], [_demoted("P3")]), IDS, "v2")
        assert any("classification" in problem and "not one of" in problem
                   for problem in problems)

    def test_an_alternative_relation_is_valid(self):
        relation = _relation("R1", ["P1", "P2"], classification="alternative")
        assert CRITIC.shape_problems(_critic([relation], [_demoted("P3")]), IDS, "v2") == []

    def test_v1_predates_classification_and_does_not_require_it(self):
        relation = _relation("R1", ["P1", "P2"])
        relation.pop("classification")
        critique = _critic([relation], [_demoted("P3")])
        assert CRITIC.shape_problems(critique, IDS, "v1") == []
        assert CRITIC.shape_problems(critique, IDS, "v2")

    def test_absorbed_is_a_v1_disposition_only(self):
        critique = _critic([_relation("R1", ["P1", "P2"])], [_demoted("P3", "absorbed")])
        assert CRITIC.shape_problems(critique, IDS, "v1") == []
        assert any("is not one of" in problem
                   for problem in CRITIC.shape_problems(critique, IDS, "v2"))

    def test_the_transition_counts_primary_and_alternative(self):
        relations = [
            _relation("R1", ["P1", "P2"]),
            _relation("R2", ["P3"], classification="alternative",
                      supporting_arguments=[{"from_proposition": "P3", "argument": "x"}]),
        ]
        step = CRITIC.transition(_curator(), _critic(relations, []), "v2")
        assert step["primary"] == 1
        assert step["alternative"] == 1


def _light_relation(identifier, sources, **overrides):
    relation = {
        "id": identifier,
        "side_a": "the man in the red coat",
        "side_b": "the snowy forest",
        "from_propositions": list(sources),
        "why_these_are_one_relation": "both reach the same warm-against-cold claim",
        "supporting_arguments": [
            {"from_proposition": source, "argument": f"argument from {source}"}
            for source in sources
        ],
        "curatorial_significance": "it is the frame's principal opposition",
        "two_colour_relevance": "the opposition would remain legible",
        "figure_side": "side_a",
        "orientation_reasoning": "the man acts; the field receives",
        "uncertainty": "the treeline may belong to neither side",
    }
    relation.update(overrides)
    return relation


def _light(relations, unused):
    return {
        "reading_of_the_set": "the proposals converge on one opposition",
        "relations": relations,
        "unused_propositions": list(unused),
        "set_judgment": "one relation carries the frame",
    }


class TestLightSchema:
    def _well_formed(self):
        return _light([_light_relation("R1", ["P1", "P2"])],
                      [{"id": "P3", "reason": "merely noticeable"}])

    def test_a_well_formed_v3_critique_has_no_problems(self):
        assert CRITIC.shape_problems(self._well_formed(), IDS, "v3") == []

    def test_v3_requires_both_set_level_fields(self):
        for field in ("reading_of_the_set", "set_judgment"):
            critique = self._well_formed()
            critique[field] = "  "
            problems = CRITIC.shape_problems(critique, IDS, "v3")
            assert any(f"{field} is empty" in problem for problem in problems)

    def test_v3_relation_fields_are_required(self):
        critique = _light([_light_relation("R1", ["P1", "P2"], two_colour_relevance="")],
                          [{"id": "P3", "reason": "merely noticeable"}])
        problems = CRITIC.shape_problems(critique, IDS, "v3")
        assert any("missing two_colour_relevance" in problem for problem in problems)

    def test_v3_has_no_classification_or_disposition(self):
        relation = _light_relation("R1", ["P1", "P2"], classification="primary")
        critique = _light([relation], [{"id": "P3", "reason": "peripheral"}])
        assert CRITIC.shape_problems(critique, IDS, "v3") == []

    def test_unused_needs_a_known_id_and_a_reason(self):
        critique = _light([_light_relation("R1", ["P1", "P2"])],
                          [{"id": "P9", "reason": "x"}, {"id": "P3", "reason": ""}])
        problems = CRITIC.shape_problems(critique, IDS, "v3")
        assert any("unknown proposition 'P9'" in problem for problem in problems)
        assert any("has no reason" in problem for problem in problems)

    def test_a_proposition_cannot_be_both_used_and_unused(self):
        critique = _light([_light_relation("R1", ["P1", "P2"])],
                          [{"id": "P1", "reason": "contributes to R1"},
                           {"id": "P3", "reason": "peripheral"}])
        problems = CRITIC.shape_problems(critique, IDS, "v3")
        assert any("both used and unused: ['P1']" in problem for problem in problems)

    def test_every_proposition_must_be_accounted_for(self):
        critique = _light([_light_relation("R1", ["P1", "P2"])], [])
        problems = CRITIC.shape_problems(critique, IDS, "v3")
        assert any("never accounted for: ['P3']" in problem for problem in problems)

    def test_supporting_one_proposition_to_two_relations_is_allowed(self):
        relations = [
            _light_relation("R1", ["P1", "P2"]),
            _light_relation("R2", ["P2", "P3"]),
        ]
        assert CRITIC.shape_problems(_light(relations, []), IDS, "v3") == []
        step = CRITIC.transition(_curator(), _light(relations, []), "v3")
        assert step["propositions_supporting_multiple_relations"] == ["P2"]

    def test_the_transition_reads_the_unused_list(self):
        step = CRITIC.transition(_curator(), self._well_formed(), "v3")
        assert step["stage_b_relations"] == 1
        assert step["unused"] == [
            {"proposition": "P3", "reason": "merely noticeable", "disposition": None}
        ]
        assert step["primary"] == 0 and step["alternative"] == 0

    def test_the_classic_schema_still_validates_under_its_own_version(self):
        assert CRITIC.schema_of("v1") == "classic"
        assert CRITIC.schema_of("v2") == "classified"
        assert CRITIC.schema_of("v3") == "light"


class TestTransition:
    def test_the_transition_counts_what_the_critic_did(self):
        step = CRITIC.transition(_curator(), _well_formed(), "v2")
        assert step["stage_a_propositions"] == 3
        assert step["stage_b_relations"] == 1
        assert step["collapsed_by"] == 2
        assert step["largest_consolidation"] == 2
        assert step["orientation"] == {"claimed": 1, "unresolved": 0}
        assert step["unused"][0]["proposition"] == "P3"

    def test_stage_a_self_declared_overlap_is_carried_into_the_report(self):
        assert CRITIC.transition(_curator(), _well_formed(), "v2")["stage_a_declared_shared_regions"] == 1

    def test_a_relation_reusing_stage_a_wording_is_not_marked_restated(self):
        step = CRITIC.transition(_curator(), _well_formed(), "v2")
        assert step["relations_restated_in_critic_terms"] == 0

    def test_a_relation_in_the_critics_own_terms_is_marked_restated(self):
        relation = _relation(
            "R1", ["P1", "P2"],
            side_a="the assertive human presence",
            side_b="the indifferent encircling wilderness",
        )
        step = CRITIC.transition(_curator(), _critic([relation], [_demoted("P3")]), "v2")
        assert step["relations_restated_in_critic_terms"] == 1

    def test_unresolved_orientation_is_counted_separately(self):
        relation = _relation("R1", ["P1", "P2"], figure_side="unresolved")
        step = CRITIC.transition(_curator(), _critic([relation], [_demoted("P3")]), "v2")
        assert step["orientation"] == {"claimed": 0, "unresolved": 1}
