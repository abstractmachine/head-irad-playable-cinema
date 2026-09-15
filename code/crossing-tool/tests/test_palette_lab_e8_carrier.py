from scripts.palette_lab import e8_carrier as CARR


def _reading():
    return {
        "what_stands_out": [{
            "entity": "the man in the red coat",
            "against": "the snowy environment and the other man",
            "what_makes_it_stand_out": "costume colour against a muted setting",
            "why_it_matters": "the film marks him as a figure of consequence",
        }],
        "strongest_cue": {
            "visible_cue": "the red coat",
            "spectator_inference": "this character is significant",
            "genre_context": "bright clothing marks a man of consequence",
            "critical_context": "the convention of coding bright colour as importance",
        },
        "organizing_distinctions": [{
            "distinction": "a figure of consequence versus a figure of passivity",
            "side_a": "the man in the red coat",
            "side_b": "the man in the muted coat and the snowy environment",
            "narratological_reason": "the frame singles one figure out",
            "formal_carrier": "colour contrast",
            "two_colour_potential": "yes",
            "colour_role_a": "red",
            "colour_role_b": "black and white",
            "what_two_colours_would_preserve": "the singling out",
            "what_two_colours_would_lose": "the posture of the other man",
        }],
        "overall_reading": "one figure is held apart from a field",
        "uncertainty": "whether the setting is doing narrative work",
    }


def _side(material="the red coat", **overrides):
    side = {
        "visible_material": material,
        "qualitative_colour_or_tone": "a saturated warm garment",
        "what_it_carries": "the figure singled out",
        "why_this_material": "it belongs to the side it carries",
    }
    side.update(overrides)
    return side


def _carrier(strength="strong", **overrides):
    carrier = {
        "source_distinction": "a figure of consequence versus a figure of passivity",
        "narratological_meaning": "the frame marks one figure as consequential",
        "formal_carrier": "costume colour against the surrounding ground",
        "two_colour_strength": strength,
        "carrier_a": _side(),
        "carrier_b": _side("the snow underfoot", what_it_carries="the field he is set against"),
        "what_the_pair_preserves": "the singling out",
        "what_the_pair_loses": "the posture and the direction of the look",
        "uncertainty": "whether the snow or the treeline is the better countercarrier",
    }
    carrier.update(overrides)
    return carrier


def _record(**overrides):
    record = {
        "image_reading": "one figure is held apart from the field around him",
        "carriers": [_carrier()],
    }
    record.update(overrides)
    return record


class TestRenderReading:
    def test_the_whole_frozen_reading_is_passed_through(self):
        text = CARR.render_reading(_reading())
        assert "WHAT STANDS OUT" in text
        assert "THE STRONGEST CUE" in text
        assert "the man in the red coat" in text
        assert "a figure of consequence versus a figure of passivity" in text

    def test_the_readers_own_doubt_is_shown(self):
        text = CARR.render_reading(_reading())
        assert "whether the setting is doing narrative work" in text


class TestShape:
    def test_a_well_formed_record_has_no_problems(self):
        assert CARR.shape_problems(_record(), 1) == []

    def test_unparsed_responses_are_reported(self):
        assert CARR.shape_problems(None, 1) == ["response did not parse as a JSON object"]

    def test_image_reading_is_required(self):
        problems = CARR.shape_problems(_record(image_reading="  "), 1)
        assert any("image_reading is empty" in problem for problem in problems)

    def test_one_carrier_per_source_distinction(self):
        problems = CARR.shape_problems(_record(), 2)
        assert any("for 2 source distinctions" in problem for problem in problems)

    def test_missing_carrier_prose_is_named(self):
        problems = CARR.shape_problems(_record(carriers=[_carrier(uncertainty="")]), 1)
        assert any("missing uncertainty" in problem for problem in problems)

    def test_missing_side_prose_is_named(self):
        carrier = _carrier(carrier_b=_side("the snow", why_this_material=""))
        problems = CARR.shape_problems(_record(carriers=[carrier]), 1)
        assert any("carrier_b missing why_this_material" in problem
                   for problem in problems)


class TestStrength:
    def test_the_vocabulary_is_enforced(self):
        problems = CARR.shape_problems(_record(carriers=[_carrier("weak")]), 1)
        assert any("two_colour_strength" in problem and "not one of" in problem
                   for problem in problems)

    def test_every_allowed_strength_parses(self):
        for strength in ("strong", "partial"):
            assert CARR.shape_problems(_record(carriers=[_carrier(strength)]), 1) == []

    def test_insufficient_may_give_no_pair(self):
        carrier = _carrier("insufficient", carrier_a=None, carrier_b=None,
                           what_the_pair_preserves="")
        assert CARR.shape_problems(_record(carriers=[carrier]), 1) == []

    def test_a_missing_pair_needs_the_insufficient_verdict(self):
        carrier = _carrier("strong", carrier_a=None, carrier_b=None)
        problems = CARR.shape_problems(_record(carriers=[carrier]), 1)
        assert any("gives no pair but is not" in problem for problem in problems)

    def test_half_a_pair_is_reported(self):
        carrier = _carrier("strong", carrier_b=None)
        problems = CARR.shape_problems(_record(carriers=[carrier]), 1)
        assert any("names one side of the pair but not the other" in problem
                   for problem in problems)

    def test_a_usable_pair_must_say_what_it_preserves(self):
        carrier = _carrier("strong", what_the_pair_preserves="")
        problems = CARR.shape_problems(_record(carriers=[carrier]), 1)
        assert any("has no what_the_pair_preserves" in problem for problem in problems)


class TestMaterialNotColour:
    def test_a_bare_colour_name_is_not_a_material(self):
        for bare in ("red", "the dark tones", "warm and pale"):
            carrier = _carrier(carrier_a=_side(bare))
            problems = CARR.shape_problems(_record(carriers=[carrier]), 1)
            assert any("names a colour, not a material" in problem
                       for problem in problems), bare

    def test_a_coloured_thing_is_a_material(self):
        for material in ("the red coat", "snow-covered ground", "the dark cave interior"):
            carrier = _carrier(carrier_a=_side(material))
            assert CARR.shape_problems(_record(carriers=[carrier]), 1) == [], material

    def test_colour_values_are_reported(self):
        carrier = _carrier(carrier_a=_side(qualitative_colour_or_tone="#9b120c"))
        problems = CARR.shape_problems(_record(carriers=[carrier]), 1)
        assert any("contains a colour value" in problem for problem in problems)

    def test_numbers_are_reported(self):
        carrier = _carrier(what_the_pair_loses="about 2 thirds of the meaning")
        problems = CARR.shape_problems(_record(carriers=[carrier]), 1)
        assert any("contains a number" in problem for problem in problems)


class TestSummary:
    def test_the_carrier_pass_is_tallied(self):
        summary = CARR.summarise(_record(), _reading())
        assert summary["source_distinctions"] == 1
        assert summary["carriers"] == 1
        assert summary["strength"] == {"strong": 1}
        assert summary["rows"][0]["materials"] == ["the red coat", "the snow underfoot"]

    def test_the_narratology_colour_roles_are_kept_for_comparison(self):
        summary = CARR.summarise(_record(), _reading())
        assert summary["rows"][0]["narratology_colour_roles"] == ["red", "black and white"]

    def test_a_material_drawn_from_its_own_side_is_observed_as_an_echo(self):
        summary = CARR.summarise(_record(), _reading())
        assert summary["rows"][0]["material_echoes_source_side"][0] is True

    def test_a_material_from_outside_its_side_is_observed_as_no_echo(self):
        carrier = _carrier(carrier_a=_side("the lantern glass"))
        summary = CARR.summarise(_record(carriers=[carrier]), _reading())
        assert summary["rows"][0]["material_echoes_source_side"][0] is False

    def test_a_missing_pair_is_counted(self):
        carrier = _carrier("insufficient", carrier_a=None, carrier_b=None)
        summary = CARR.summarise(_record(carriers=[carrier]), _reading())
        assert summary["pairs_without_a_carrier"] == 1

    def test_an_unparsed_response_summarises_without_raising(self):
        summary = CARR.summarise(None, _reading())
        assert summary["carriers"] == 0
        assert summary["source_distinctions"] == 1
