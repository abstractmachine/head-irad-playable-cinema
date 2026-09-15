from scripts.palette_lab import human_review as HR


KNOWN = {"C01", "C02", "C03", "C09", "C10", "C11"}


def _role(mode, candidates, preferred=None, label=""):
    role = {"mode": mode, "candidates": list(candidates), "stated_label": label}
    if preferred is not None:
        role["preferred"] = preferred
    return role


def _record(**overrides):
    record = {
        "id": "PAL-015",
        "reviewer": "Douglas Edric Stanley",
        "reviewed": True,
        "role_vocabulary": HR.ROLE_VOCABULARY,
        "preferred": {
            "figure": _role("single", ["C02"], label="man in red coat"),
            "ground": _role("single", ["C09"], label="snowy ground"),
            "rationale": "strongest tonal opposition",
        },
        "alternatives": [],
        "candidate_pool": {"sufficiency": "sufficient", "reasons": [], "notes": ""},
    }
    record.update(overrides)
    return record


def _pool():
    return {
        "cand:C01": {"label": "man in cave", "hex": "#0f0908", "lab": [2.8, 1.0, 1.5]},
        "cand:C02": {"label": "man in red coat", "hex": "#591010", "lab": [19.7, 40.0, 25.0]},
        "cand:C03": {"label": "desert plain", "hex": "#cbab8b", "lab": [71.7, 5.0, 20.0]},
        "cand:C09": {"label": "snowy ground", "hex": "#c7c6cc", "lab": [80.1, 1.0, -3.0]},
        "cand:C10": {"label": "cave interior", "hex": "#090605", "lab": [1.8, 0.5, 0.5]},
        "cand:C11": {"label": "cave interior", "hex": "#090605", "lab": [1.8, 0.5, 0.5]},
    }


def _mapping():
    return {review_id: f"cand:{review_id}" for review_id in KNOWN}


class TestRoleValidation:
    def test_single_requires_exactly_one_candidate(self):
        assert HR.validate_role(_role("single", ["C02"]), KNOWN, "x") == []
        assert HR.validate_role(_role("single", ["C02", "C09"]), KNOWN, "x")

    def test_alternative_and_compound_require_at_least_two(self):
        assert HR.validate_role(_role("alternative", ["C10", "C11"]), KNOWN, "x") == []
        assert HR.validate_role(_role("compound", ["C09", "C10"]), KNOWN, "x") == []
        assert HR.validate_role(_role("alternative", ["C10"]), KNOWN, "x")

    def test_unknown_review_id_is_rejected(self):
        problems = HR.validate_role(_role("single", ["C99"]), KNOWN, "x")
        assert any("not in the frozen E6 pool" in problem for problem in problems)

    def test_preferred_must_be_one_of_its_own_candidates(self):
        assert HR.validate_role(_role("alternative", ["C10", "C11"], "C11"), KNOWN, "x") == []
        assert HR.validate_role(_role("alternative", ["C10", "C11"], "C03"), KNOWN, "x")

    def test_a_compound_role_cannot_have_a_preferred_member(self):
        problems = HR.validate_role(_role("compound", ["C09", "C10"], "C09"), KNOWN, "x")
        assert any("compound role cannot have a preferred" in problem for problem in problems)

    def test_unknown_mode_is_rejected(self):
        assert HR.validate_role(_role("best", ["C02"]), KNOWN, "x")


class TestRecordValidation:
    def test_a_faithful_record_validates(self):
        assert HR.validate_record(_record(), KNOWN) == []

    def test_role_vocabulary_must_be_declared(self):
        assert HR.validate_record(_record(role_vocabulary="focus_ambiance"), KNOWN)

    def test_extended_sufficiency_state_is_accepted(self):
        record = _record(candidate_pool={
            "sufficiency": "pieces_present_grouping_absent",
            "reasons": ["perceptual_role_requires_compound_region"],
            "notes": "",
        })
        assert HR.validate_record(record, KNOWN) == []

    def test_unknown_sufficiency_or_reason_is_rejected(self):
        assert HR.validate_record(_record(candidate_pool={
            "sufficiency": "maybe", "reasons": [], "notes": ""}), KNOWN)
        assert HR.validate_record(_record(candidate_pool={
            "sufficiency": "sufficient", "reasons": ["invented"], "notes": ""}), KNOWN)

    def test_alternatives_are_validated_too(self):
        record = _record(alternatives=[{
            "figure": _role("single", ["C99"]),
            "ground": _role("single", ["C03"]),
        }])
        assert HR.validate_record(record, KNOWN)


class TestThreeDistinctStates:
    def test_or_and_and_are_not_flattened_to_the_same_thing(self):
        alternative = {"figure": _role("single", ["C02"]),
                       "ground": _role("alternative", ["C10", "C11"]), "rationale": ""}
        compound = {"figure": _role("single", ["C02"]),
                    "ground": _role("compound", ["C10", "C11"]), "rationale": ""}
        assert HR.reading_kind(alternative) == "alternative_pair"
        assert HR.reading_kind(compound) == "representation_mismatch"

    def test_single_single_is_an_exact_pair(self):
        reading = {"figure": _role("single", ["C02"]),
                   "ground": _role("single", ["C09"]), "rationale": ""}
        assert HR.reading_kind(reading) == "exact_single_pair"

    def test_compound_roles_are_not_comparable(self):
        assert HR.role_is_comparable(_role("single", ["C02"])) is True
        assert HR.role_is_comparable(_role("alternative", ["C10", "C11"])) is True
        assert HR.role_is_comparable(_role("compound", ["C10", "C11"])) is False


class TestReadings:
    def test_preferred_comes_first_then_alternatives(self):
        record = _record(alternatives=[{
            "figure": _role("single", ["C01"]),
            "ground": _role("single", ["C03"]),
            "rationale": "narrative figurehood",
        }])
        out = HR.readings(record)
        assert [reading["kind"] for reading in out] == ["preferred", "alternative"]
        assert out[0]["index"] == 0 and out[1]["index"] == 1
        assert out[1]["figure"]["candidates"] == ["C01"]

    def test_resolve_maps_review_ids_onto_candidate_ids(self):
        assert HR.resolve(_role("alternative", ["C10", "C11"]), _mapping()) == [
            "cand:C10", "cand:C11"
        ]


class TestCompareReading:
    def _positions(self):
        return {
            ("cand:C02", "cand:C09"): 11,
            ("cand:C09", "cand:C02"): 31,
            ("cand:C03", "cand:C10"): 3,
            ("cand:C03", "cand:C11"): 7,
            ("cand:C10", "cand:C03"): 18,
            ("cand:C11", "cand:C03"): 25,
        }

    def test_compound_role_reports_representation_mismatch_without_guessing(self):
        reading = {"kind": "preferred", "index": 0,
                   "figure": _role("compound", ["C09", "C10"]),
                   "ground": _role("single", ["C03"])}
        result = HR.compare_reading(reading, self._positions(), _mapping(), _pool(), 100)
        assert result["comparison"] == "representation_mismatch"
        assert result["stated_orientation"] is None
        assert "compound" in result["reason"]

    def test_alternative_role_earns_credit_for_its_best_member(self):
        reading = {"kind": "preferred", "index": 0,
                   "figure": _role("single", ["C03"]),
                   "ground": _role("alternative", ["C10", "C11"])}
        result = HR.compare_reading(reading, self._positions(), _mapping(), _pool(), 100)
        assert result["comparison"] == "alternative_pair"
        assert result["stated_orientation"]["rank"] == 3
        assert result["stated_orientation"]["ground_candidate_id"] == "cand:C10"

    def test_orientation_is_measured_on_the_reviewers_own_candidates(self):
        reading = {"kind": "preferred", "index": 0,
                   "figure": _role("single", ["C02"]),
                   "ground": _role("single", ["C09"])}
        result = HR.compare_reading(reading, self._positions(), _mapping(), _pool(), 100)
        assert result["stated_orientation"]["rank"] == 11
        assert result["reversed_orientation"]["rank"] == 31
        assert result["orientation_preferred_by_model"] == "stated"
        assert result["orientation_rank_gap"] == 20

    def test_an_unrankable_reading_reports_none_rather_than_a_guess(self):
        reading = {"kind": "preferred", "index": 0,
                   "figure": _role("single", ["C01"]),
                   "ground": _role("single", ["C09"])}
        result = HR.compare_reading(reading, self._positions(), _mapping(), _pool(), 100)
        assert result["stated_orientation"] is None


class TestTopPairDistance:
    def test_swapped_orientation_is_detected_by_measurement(self):
        top = {"figure_id": "cand:C09", "ground_id": "cand:C02"}
        human = {"figure_candidate_id": "cand:C02", "ground_candidate_id": "cand:C09"}
        distance = HR.top_pair_distance(top, human, _pool())
        assert distance["closer_when_swapped"] is True
        assert distance["worst_delta_e_swapped_orientation"] == 0.0


class TestSummary:
    def _image(self, image_id, comparison, stated_rank, reversed_rank, swapped):
        view = {
            "stated_orientation": (
                {"rank": stated_rank, "of": 100} if stated_rank else None
            ),
            "reversed_orientation": {"rank": reversed_rank, "of": 100},
            "orientation_preferred_by_model": (
                "stated" if stated_rank and stated_rank <= reversed_rank else "reversed"
            ),
            "top_pair_distance": {
                "worst_delta_e_same_orientation": 40.0 if swapped else 1.0,
                "worst_delta_e_swapped_orientation": 1.0 if swapped else 40.0,
                "closer_when_swapped": swapped,
            },
        }
        return {
            "id": image_id,
            "readings": [{
                "comparison": comparison,
                "per_hypothesis": {"R2_conjunctive": view},
            }],
        }

    def test_mismatched_images_are_listed_and_excluded_from_ranks(self):
        images = [
            self._image("PAL-015", "exact_single_pair", 11, 31, False),
            self._image("PAL-004", "representation_mismatch", None, 1, False),
        ]
        summary = HR.summarise(images, "R2_conjunctive")
        assert summary["images_compared"] == 1
        assert summary["images_with_representation_mismatch"] == ["PAL-004"]

    def test_orientation_counts_are_reported_per_image(self):
        images = [
            self._image("PAL-015", "exact_single_pair", 11, 31, False),
            self._image("PAL-030", "exact_single_pair", 326, 179, False),
        ]
        summary = HR.summarise(images, "R2_conjunctive")
        assert summary["stated_orientation_preferred"] == ["PAL-015"]
        assert summary["reversed_orientation_preferred"] == ["PAL-030"]

    def test_relation_recovery_is_separate_from_orientation(self):
        images = [self._image("PAL-015", "exact_single_pair", 11, 31, True)]
        summary = HR.summarise(images, "R2_conjunctive")
        assert summary["top_pair_recovers_the_relation"] == 1
        assert summary["top_pair_also_orients_it_as_stated"] == 0
