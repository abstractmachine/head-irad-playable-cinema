from scripts.palette_lab import e8_spatializer as SPAT


def _referent(phrase, kind="whole", covers="the whole figure"):
    return {"phrase": phrase, "relation_to_concept": kind, "covers": covers}


def _side(concept, referents, **overrides):
    side = {
        "concept": concept,
        "referents": list(referents),
        "measurable_expectations": "a localized region with a warm chromatic character",
        "interpretive_content": "human agency and bearing",
    }
    side.update(overrides)
    return side


def _spec(**overrides):
    spec = {
        "relation_id": "R1",
        "side_a": _side("the red-coated man", [_referent("man in red coat")]),
        "side_b": _side(
            "the snowy environment",
            [_referent("snow-covered ground", "broader_field", "the open field"),
             _referent("snow", "material", "the substance, wherever it lies")],
            measurable_expectations="a large pale field spread across the frame",
            interpretive_content="cold, winter, environmental adversity",
        ),
        "relation": {
            "relational_term": "isolated against",
            "measurable_proxies": "neighbourhood, relative extent, colour difference",
            "interpretive_content": "psychological isolation",
        },
        "uncertainty": "whether the snow phrase will return the forest floor too",
    }
    spec.update(overrides)
    return spec


def _critic_relation():
    return {
        "id": "R1",
        "side_a": "The man in the red coat, as a visually dominant presence",
        "side_b": "The snowy forest and the horse, as a muted environment",
        "figure_side": "side_a",
        "why_these_are_one_relation": "both reach the same claim",
        "curatorial_significance": "the frame's principal opposition",
        "two_colour_relevance": "it would survive reduction",
        "orientation_reasoning": "the man acts",
        "uncertainty": "the horse may belong to neither side",
        "supporting_arguments": [
            {"from_proposition": "P1", "argument": "chromatic opposition"},
            {"from_proposition": "P4", "argument": "spatial containment"},
        ],
    }


def _propositions():
    return [
        {"id": "P1", "side_a": "the man in the red coat", "side_b": "the snowy forest",
         "relationship": "warm against cold"},
        {"id": "P4", "side_a": "the snowy ground", "side_b": "the two men",
         "relationship": "field against figures"},
    ]


class TestRenderRelation:
    def test_the_relation_and_its_provenance_are_both_passed_through(self):
        text = SPAT.render_relation(_critic_relation(), _propositions())
        assert "Relation R1" in text
        assert "The man in the red coat" in text
        assert "[P1]" in text and "[P4]" in text
        assert "chromatic opposition" in text

    def test_the_critics_own_uncertainty_is_shown(self):
        text = SPAT.render_relation(_critic_relation(), _propositions())
        assert "the horse may belong to neither side" in text


class TestShape:
    def test_a_well_formed_specification_has_no_problems(self):
        assert SPAT.shape_problems(_spec(), "R1") == []

    def test_unparsed_responses_are_reported(self):
        assert SPAT.shape_problems(None, "R1") == [
            "response did not parse as a JSON object"
        ]

    def test_a_mismatched_relation_id_is_reported(self):
        problems = SPAT.shape_problems(_spec(relation_id="R2"), "R1")
        assert any("does not match" in problem for problem in problems)

    def test_missing_side_prose_is_named(self):
        spec = _spec(side_a=_side("the man", [_referent("man in red coat")],
                                  interpretive_content="  "))
        problems = SPAT.shape_problems(spec, "R1")
        assert any("side_a missing interpretive_content" in problem
                   for problem in problems)

    def test_missing_relation_prose_is_named(self):
        spec = _spec()
        spec["relation"]["measurable_proxies"] = ""
        problems = SPAT.shape_problems(spec, "R1")
        assert any("relation missing measurable_proxies" in problem
                   for problem in problems)

    def test_empty_uncertainty_is_reported(self):
        problems = SPAT.shape_problems(_spec(uncertainty=" "), "R1")
        assert any("uncertainty is empty" in problem for problem in problems)

    def test_numbers_in_expectations_are_reported(self):
        spec = _spec(side_a=_side(
            "the man", [_referent("man in red coat")],
            measurable_expectations="covers 20 percent of the frame",
        ))
        problems = SPAT.shape_problems(spec, "R1")
        assert any("contains a number" in problem for problem in problems)

    def test_relation_ids_in_prose_are_not_mistaken_for_numbers(self):
        spec = _spec()
        spec["relation"]["measurable_proxies"] = "as in R1, neighbourhood and extent"
        assert SPAT.shape_problems(spec, "R1") == []


class TestReferents:
    def test_relation_to_concept_must_come_from_the_vocabulary(self):
        spec = _spec(side_a=_side("the man", [_referent("man in red coat", "sort of")]))
        problems = SPAT.shape_problems(spec, "R1")
        assert any("is not one of" in problem for problem in problems)

    def test_every_vocabulary_value_is_accepted(self):
        for kind in SPAT.RELATION_TO_CONCEPT:
            spec = _spec(side_a=_side("the man", [_referent("man in red coat", kind)]))
            assert SPAT.shape_problems(spec, "R1") == []

    def test_a_referent_needs_a_phrase_a_kind_and_what_it_covers(self):
        spec = _spec(side_a=_side(
            "the man", [{"phrase": "man in red coat", "relation_to_concept": "whole"}]
        ))
        problems = SPAT.shape_problems(spec, "R1")
        assert any("missing covers" in problem for problem in problems)

    def test_a_conjunctive_phrase_is_reported(self):
        spec = _spec(side_a=_side("the environment", [_referent(
            "the snowy forest and the horse"
        )]))
        problems = SPAT.shape_problems(spec, "R1")
        assert any("is conjunctive" in problem for problem in problems)

    def test_a_hyphenated_compound_is_not_conjunctive(self):
        spec = _spec(side_a=_side("the other man", [_referent(
            "man wearing black-and-white coat"
        )]))
        assert SPAT.shape_problems(spec, "R1") == []

    def test_an_overlong_phrase_is_reported(self):
        spec = _spec(side_a=_side("the man", [_referent(
            "that solitary weathered figure standing quietly beside a tethered horse"
        )]))
        problems = SPAT.shape_problems(spec, "R1")
        assert any("long for a segmentation request" in problem
                   for problem in problems)

    def test_a_side_with_no_referents_is_allowed(self):
        spec = _spec(side_a=_side("enclosure", []))
        assert SPAT.shape_problems(spec, "R1") == []


class TestSummary:
    def test_a_multi_referent_side_is_marked_compound(self):
        summary = SPAT.summarise(_spec(), _critic_relation())
        assert summary["sides"]["side_a"]["compound"] is False
        assert summary["sides"]["side_b"]["compound"] is True
        assert summary["sides"]["side_b"]["referent_kinds"] == {
            "broader_field": 1, "material": 1
        }

    def test_a_side_with_no_referents_is_marked_unlocatable(self):
        summary = SPAT.summarise(_spec(side_a=_side("enclosure", [])), _critic_relation())
        assert summary["sides"]["side_a"]["unlocatable"] is True
        assert summary["sides"]["side_a"]["referents"] == 0

    def test_the_critic_side_is_kept_beside_the_extracted_concept(self):
        summary = SPAT.summarise(_spec(), _critic_relation())
        side = summary["sides"]["side_a"]
        assert side["concept"] == "the red-coated man"
        assert side["critic_side"].startswith("The man in the red coat")

    def test_absent_relational_proxies_are_visible(self):
        spec = _spec()
        spec["relation"]["measurable_proxies"] = ""
        summary = SPAT.summarise(spec, _critic_relation())
        assert summary["has_measurable_proxies"] is False
        assert summary["has_relation_interpretive_content"] is True

    def test_distinct_phrases_are_counted_across_both_sides(self):
        assert SPAT.summarise(_spec(), _critic_relation())["distinct_phrases"] == 3

    def test_an_unparsed_response_summarises_without_raising(self):
        summary = SPAT.summarise(None, _critic_relation())
        assert summary["sides"]["side_a"]["unlocatable"] is True
        assert summary["relational_term"] is None
