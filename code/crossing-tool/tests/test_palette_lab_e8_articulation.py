from scripts.palette_lab import e8_articulation as ART


def _reading():
    return {
        "what_stands_out": [{
            "entity": "the lit doorway",
            "against": "the unlit street",
            "what_makes_it_stand_out": "it is the only warm opening in the row",
            "why_it_matters": "the film puts the arrival there",
        }],
        "strongest_cue": {
            "visible_cue": "the doorway",
            "spectator_inference": "someone is expected",
            "genre_context": "arrivals are staged at thresholds",
            "critical_context": "the convention of the lit threshold",
        },
        "organizing_distinctions": [{
            "distinction": "shelter against the street",
            "side_a": "the doorway and the person waiting in it",
            "side_b": "the roadway and the row of shuttered fronts",
            "narratological_reason": "the frame sets one against the other",
            "formal_carrier": "illumination",
            "two_colour_potential": "yes",
            "colour_role_a": "warm",
            "colour_role_b": "dark",
            "what_two_colours_would_preserve": "the threshold",
            "what_two_colours_would_lose": "the waiting",
        }],
        "overall_reading": "a threshold held against a street",
        "uncertainty": "whether the street is doing narrative work",
    }


def _carrier():
    return {
        "image_reading": "a threshold set against a street",
        "carriers": [{
            "source_distinction": "shelter against the street",
            "narratological_meaning": "one place is offered and the other is not",
            "formal_carrier": "illumination",
            "two_colour_strength": "partial",
            "carrier_a": {
                "visible_material": "the lit doorway",
                "qualitative_colour_or_tone": "a warm opening",
                "what_it_carries": "the shelter",
                "why_this_material": "it belongs to the side it carries",
            },
            "carrier_b": {
                "visible_material": "the shuttered shopfronts",
                "qualitative_colour_or_tone": "flat and unlit",
                "what_it_carries": "the street",
                "why_this_material": "it belongs to the side it carries",
            },
            "what_the_pair_preserves": "the threshold",
            "what_the_pair_loses": "the waiting",
            "uncertainty": "whether the roadway is the better counterpart",
        }],
    }


def _pair(**overrides):
    pair = {
        "distinguished_material": "the lit doorway",
        "counterfield_material": "rain-slick asphalt",
        "relationship": "broader_field",
        "why_this_pair": "the doorway takes its force from the roadway, not the shutters",
    }
    pair.update(overrides)
    return pair


def _articulation(**overrides):
    record = {
        "source_distinction": "shelter against the street",
        "narratological_meaning": "one place is offered and the other is not",
        "previous_direct_pair": {
            "material_a": "the lit doorway",
            "material_b": "the shuttered shopfronts",
        },
        "chromatic_articulation": _pair(),
        "two_colour_strength": "partial",
        "what_the_pair_preserves": "the threshold",
        "what_the_pair_loses": "the waiting and the gesture of arrival",
        "uncertainty": "whether the asphalt reads as a field at all",
    }
    record.update(overrides)
    return record


def _record(**overrides):
    record = {
        "image_reading": "a threshold held against a street",
        "articulations": [_articulation()],
    }
    record.update(overrides)
    return record


def _problems(record, reading=None):
    reading = reading or _reading()
    return ART.shape_problems(record, reading["organizing_distinctions"])


class TestRenderInputs:
    def test_the_complete_narratology_reading_is_passed_through(self):
        text = ART.render_narratology(_reading())
        assert "WHAT STANDS OUT" in text
        assert "shelter against the street" in text
        assert "the reader's own doubt: whether the street is doing narrative work" in text

    def test_the_colour_roles_carrier_withheld_are_shown_here(self):
        text = ART.render_narratology(_reading())
        assert "chromatic role of side A: warm" in text
        assert "chromatic role of side B: dark" in text

    def test_the_complete_carrier_reading_is_passed_through(self):
        text = ART.render_carriers(_carrier())
        assert "the lit doorway" in text
        assert "the shuttered shopfronts" in text
        assert "how strong it judged the pair: partial" in text

    def test_a_carrier_pass_that_named_no_pair_still_renders(self):
        carrier = _carrier()
        carrier["carriers"][0]["carrier_b"] = None
        assert "material B: none named" in ART.render_carriers(carrier)


class TestShape:
    def test_a_well_formed_record_has_no_problems(self):
        assert _problems(_record()) == []

    def test_unparsed_responses_are_reported(self):
        assert _problems(None) == ["response did not parse as a JSON object"]

    def test_image_reading_is_required(self):
        assert any("image_reading is empty" in problem
                   for problem in _problems(_record(image_reading="  ")))

    def test_one_articulation_per_source_distinction(self):
        record = _record(articulations=[_articulation(), _articulation()])
        assert any("2 articulations for 1 source distinctions" in problem
                   for problem in _problems(record))

    def test_missing_prose_is_named(self):
        record = _record(articulations=[_articulation(uncertainty="")])
        assert any("missing uncertainty" in problem for problem in _problems(record))

    def test_missing_pair_prose_is_named(self):
        record = _record(articulations=[_articulation(
            chromatic_articulation=_pair(why_this_pair=""))])
        assert any("missing why_this_pair" in problem for problem in _problems(record))


class TestSourceDistinctionPreserved:
    def test_a_copied_distinction_passes(self):
        assert _problems(_record()) == []

    def test_punctuation_and_case_do_not_count_as_drift(self):
        record = _record(articulations=[_articulation(
            source_distinction="Shelter Against The Street.")])
        assert _problems(record) == []

    def test_a_replaced_distinction_is_reported(self):
        record = _record(articulations=[_articulation(
            source_distinction="warm against dark")])
        assert any("source_distinction was not copied" in problem
                   for problem in _problems(record))


class TestRelationship:
    def test_the_vocabulary_is_enforced(self):
        record = _record(articulations=[_articulation(
            chromatic_articulation=_pair(relationship="environmental"))])
        assert any("relationship" in problem and "is not one of" in problem
                   for problem in _problems(record))

    def test_every_allowed_relationship_parses(self):
        for name in ART.RELATIONSHIPS:
            record = _record(articulations=[_articulation(
                chromatic_articulation=_pair(relationship=name))])
            assert _problems(record) == [], name


class TestStrength:
    def test_the_vocabulary_is_enforced(self):
        record = _record(articulations=[_articulation(two_colour_strength="weak")])
        assert any("two_colour_strength" in problem and "is not one of" in problem
                   for problem in _problems(record))

    def test_every_allowed_strength_parses(self):
        for name in ("strong", "partial"):
            record = _record(articulations=[_articulation(two_colour_strength=name)])
            assert _problems(record) == [], name

    def test_insufficient_may_give_no_pair(self):
        record = _record(articulations=[_articulation(
            two_colour_strength="insufficient", chromatic_articulation=None,
            what_the_pair_preserves="")])
        assert _problems(record) == []

    def test_a_missing_pair_needs_the_insufficient_verdict(self):
        record = _record(articulations=[_articulation(chromatic_articulation=None)])
        assert any("gives no pair but is not" in problem
                   for problem in _problems(record))

    def test_a_usable_pair_must_say_what_it_preserves(self):
        record = _record(articulations=[_articulation(what_the_pair_preserves="")])
        assert any("has no what_the_pair_preserves" in problem
                   for problem in _problems(record))


class TestMaterials:
    def test_both_materials_are_required_when_a_pair_exists(self):
        for field in ART.MATERIAL_FIELDS:
            record = _record(articulations=[_articulation(
                chromatic_articulation=_pair(**{field: "  "}))])
            assert any(f"missing {field}" in problem
                       for problem in _problems(record)), field

    def test_a_bare_colour_name_is_not_a_material(self):
        for bare in ("warm", "the dark tones", "pale and muted"):
            record = _record(articulations=[_articulation(
                chromatic_articulation=_pair(counterfield_material=bare))])
            assert any("names a colour, not a material" in problem
                       for problem in _problems(record)), bare

    def test_a_coloured_thing_is_a_material(self):
        for material in ("rain-slick asphalt", "the green baize table",
                         "a plastered wall", "the black-and-white tiled floor"):
            record = _record(articulations=[_articulation(
                chromatic_articulation=_pair(counterfield_material=material))])
            assert _problems(record) == [], material

    def test_colour_values_are_reported(self):
        record = _record(articulations=[_articulation(
            chromatic_articulation=_pair(why_this_pair="it reads as #9b120c"))])
        assert any("contains a colour value" in problem
                   for problem in _problems(record))

    def test_numbers_are_reported(self):
        record = _record(articulations=[_articulation(
            what_the_pair_loses="about 2 thirds of the meaning")])
        assert any("contains a number" in problem for problem in _problems(record))


class TestConjunctiveMaterials:
    def test_joined_materials_are_reported(self):
        for joined in ("the awning and the crowd beneath it",
                       "sunlit ground and distant figures",
                       "the table & the cloth"):
            record = _record(articulations=[_articulation(
                chromatic_articulation=_pair(counterfield_material=joined))])
            assert any("joins several materials" in problem
                       for problem in _problems(record)), joined

    def test_a_hyphenated_tone_is_one_material(self):
        assert ART._is_conjunctive("the black-and-white coat") is False

    def test_a_spaced_tone_phrase_is_one_material(self):
        for phrase in ("the black and white coat", "warm and pale plaster",
                       "a red and gold banner"):
            assert ART._is_conjunctive(phrase) is False, phrase

    def test_a_word_containing_and_is_not_a_conjunction(self):
        for phrase in ("a sandy verge", "the bandstand", "an island of light"):
            assert ART._is_conjunctive(phrase) is False, phrase

    def test_an_article_after_the_conjunction_does_not_hide_the_join(self):
        assert ART._is_conjunctive("the ground and the treeline") is True


class TestProvenance:
    def test_both_frozen_inputs_are_named(self):
        sources = ART.provenance("PAL-001")
        assert sources["narratology"] == "narratology-v1/PAL-001/03-narratology.json"
        assert sources["carrier"] == "carrier-v1/PAL-001/04-carrier.json"


class TestSummary:
    def test_the_pass_is_tallied(self):
        summary = ART.summarise(_record(), _reading(), _carrier())
        assert summary["source_distinctions"] == 1
        assert summary["articulations"] == 1
        assert summary["relationship"] == {"broader_field": 1}
        assert summary["strength"] == {"partial": 1}

    def test_the_previous_materials_are_kept_beside_the_new_pair(self):
        row = ART.summarise(_record(), _reading(), _carrier())["rows"][0]
        assert row["carrier_v1_materials"] == ["the lit doorway", "the shuttered shopfronts"]
        assert row["counterfield_material"] == "rain-slick asphalt"

    def test_the_earlier_verdicts_are_kept_for_comparison(self):
        row = ART.summarise(_record(), _reading(), _carrier())["rows"][0]
        assert row["narratology_colour_roles"] == ["warm", "dark"]
        assert row["carrier_v1_strength"] == "partial"

    def test_a_missing_pair_is_counted(self):
        record = _record(articulations=[_articulation(
            two_colour_strength="insufficient", chromatic_articulation=None)])
        summary = ART.summarise(record, _reading(), _carrier())
        assert summary["articulations_without_a_pair"] == 1
        assert summary["relationship"] == {}

    def test_an_unparsed_response_summarises_without_raising(self):
        summary = ART.summarise(None, _reading(), _carrier())
        assert summary["articulations"] == 0
        assert summary["source_distinctions"] == 1
