from scripts.palette_lab import e8_narratology as NARR


def _stands_out(entity="the conspicuously dressed rider", **overrides):
    entry = {
        "entity": entity,
        "against": "the surrounding winter field",
        "what_makes_it_stand_out": "costume colour against a muted setting",
        "why_it_matters": "the film wants this figure held apart from the others",
    }
    entry.update(overrides)
    return entry


def _cue(**overrides):
    cue = {
        "visible_cue": "a drawn weapon held low",
        "spectator_inference": "a confrontation is imminent",
        "genre_context": "the standoff is a codified situation in this genre",
        "critical_context": "no problematic convention is significantly in play here",
    }
    cue.update(overrides)
    return cue


def _distinction(potential="yes", **overrides):
    entry = {
        "distinction": "exception against norm",
        "side_a": "the distinguished figure",
        "side_b": "the ordinary field around him",
        "narratological_reason": "the frame is about one person being singled out",
        "formal_carrier": "costume colour against the desaturated setting",
        "two_colour_potential": potential,
        "colour_role_a": "the costume of the distinguished figure",
        "colour_role_b": "the surrounding field",
        "what_two_colours_would_preserve": "the singling out",
        "what_two_colours_would_lose": "the posture and the direction of the look",
    }
    if potential == "no":
        entry["colour_role_a"] = ""
        entry["colour_role_b"] = ""
        entry["what_two_colours_would_preserve"] = ""
    entry.update(overrides)
    return entry


def _reading(**overrides):
    reading = {
        "what_stands_out": [_stands_out()],
        "strongest_cue": _cue(),
        "organizing_distinctions": [_distinction()],
        "overall_reading": "the frame singles one figure out from a field",
        "uncertainty": "whether the setting is doing narrative work or merely present",
    }
    reading.update(overrides)
    return reading


class TestShape:
    def test_a_well_formed_reading_has_no_problems(self):
        assert NARR.shape_problems(_reading()) == []

    def test_unparsed_responses_are_reported(self):
        assert NARR.shape_problems(None) == ["response did not parse as a JSON object"]

    def test_set_level_prose_is_required(self):
        for field in ("overall_reading", "uncertainty"):
            problems = NARR.shape_problems(_reading(**{field: "  "}))
            assert any(f"{field} is empty" in problem for problem in problems)

    def test_missing_stands_out_fields_are_named(self):
        reading = _reading(what_stands_out=[_stands_out(against="")])
        problems = NARR.shape_problems(reading)
        assert any("missing against" in problem for problem in problems)

    def test_more_than_three_stands_out_entries_is_reported(self):
        reading = _reading(what_stands_out=[_stands_out() for _ in range(4)])
        problems = NARR.shape_problems(reading)
        assert any("expected at most" in problem for problem in problems)

    def test_the_cue_block_is_required_in_full(self):
        reading = _reading(strongest_cue=_cue(critical_context=""))
        problems = NARR.shape_problems(reading)
        assert any("strongest_cue missing critical_context" in problem
                   for problem in problems)

    def test_a_distinction_is_required(self):
        problems = NARR.shape_problems(_reading(organizing_distinctions=[]))
        assert any("no organizing_distinctions" in problem for problem in problems)


class TestTwoColourPotential:
    def test_the_vocabulary_is_enforced(self):
        reading = _reading(organizing_distinctions=[_distinction("maybe")])
        problems = NARR.shape_problems(reading)
        assert any("two_colour_potential" in problem and "not one of" in problem
                   for problem in problems)

    def test_every_allowed_value_parses(self):
        for potential in NARR.TWO_COLOUR_POTENTIAL:
            assert NARR.shape_problems(
                _reading(organizing_distinctions=[_distinction(potential)])
            ) == []

    def test_claiming_colour_potential_requires_naming_both_roles(self):
        for potential in NARR.POTENTIAL_NEEDING_ROLES:
            reading = _reading(organizing_distinctions=[
                _distinction(potential, colour_role_b="")
            ])
            problems = NARR.shape_problems(reading)
            assert any("without naming both colour roles" in problem
                       for problem in problems)

    def test_rejecting_two_colours_while_naming_roles_is_reported(self):
        reading = _reading(organizing_distinctions=[
            _distinction("no", colour_role_a="the costume")
        ])
        problems = NARR.shape_problems(reading)
        assert any("rules out two colours yet names roles" in problem
                   for problem in problems)

    def test_rejecting_two_colours_must_still_say_what_is_lost(self):
        reading = _reading(organizing_distinctions=[
            _distinction("no", what_two_colours_would_lose="")
        ])
        problems = NARR.shape_problems(reading)
        assert any("missing what_two_colours_would_lose" in problem
                   for problem in problems)


class TestVocabularyLimits:
    def test_numbers_anywhere_in_prose_are_reported(self):
        reading = _reading(what_stands_out=[
            _stands_out(why_it_matters="it occupies 40 percent of the frame")
        ])
        problems = NARR.shape_problems(reading)
        assert any("contains a number" in problem for problem in problems)

    def test_measurement_vocabulary_is_reported(self):
        reading = _reading(organizing_distinctions=[
            _distinction(formal_carrier="the mask covers most of the frame")
        ])
        problems = NARR.shape_problems(reading)
        assert any("refers to measurement" in problem for problem in problems)

    def test_ordinary_colour_language_is_not_flagged(self):
        reading = _reading(organizing_distinctions=[
            _distinction(formal_carrier="a saturated red costume against pale ground")
        ])
        assert NARR.shape_problems(reading) == []


class TestSummary:
    def test_the_reading_is_tallied(self):
        summary = NARR.summarise(_reading())
        assert summary["stands_out_entries"] == 1
        assert summary["counterfields"] == ["the surrounding winter field"]
        assert summary["distinctions"] == 1
        assert summary["two_colour_potential"] == {"yes": 1}
        assert summary["rejects_two_colour"] is False
        assert summary["names_a_convention"] is True

    def test_a_rejected_colour_pair_is_visible_in_the_summary(self):
        summary = NARR.summarise(_reading(organizing_distinctions=[_distinction("no")]))
        assert summary["rejects_two_colour"] is True
        assert summary["two_colour_potential"] == {"no": 1}

    def test_distinction_vocabulary_is_collected_for_comparison(self):
        summary = NARR.summarise(_reading())
        assert summary["distinction_labels"] == ["exception against norm"]
        assert summary["formal_carriers"][0].startswith("costume colour")

    def test_an_unparsed_response_summarises_without_raising(self):
        summary = NARR.summarise(None)
        assert summary["stands_out_entries"] == 0
        assert summary["rejects_two_colour"] is False
