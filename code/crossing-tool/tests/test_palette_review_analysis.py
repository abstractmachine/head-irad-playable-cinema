import json

import pytest

from data import palette_review as store
from services import palette_review_analysis as analysis
from services import palette_review_stats as stats


def _proposal(strategy, label, materials, colours, *, active=True, statuses=None):
    statuses = statuses or ["success", "success"]
    return {
        "strategy": strategy, "label": label, "active": active,
        "materials": materials, "converges_with": [],
        "colours": colours,
        "measurement": [
            {"material": materials[i] if i < len(materials) else None,
             "operationalization": {"status": "atomic", "localization_targets": []},
             "segmentation": {"status": statuses[i], "mask_pixels": 100,
                              "coverage": 0.2, "targets": []}}
            for i in range(2)
        ],
    }


def _colour(rgb, hex_value):
    return {"rgb": rgb, "hex": hex_value, "lab": analysis.to_lab(rgb)}


def _entry():
    entry = {}
    store.record_generation(entry, "measured-review-v1", "gen-a", {
        "1": _proposal("direct", "DIRECT", ["the red coat", "the snow"],
                       [_colour([198, 40, 30], "#c6281e"),
                        _colour([200, 200, 205], "#c8c8cd")]),
        "4": _proposal("control", "CONTROL", [],
                       [_colour([10, 10, 10], "#0a0a0a"),
                        _colour([90, 60, 50], "#5a3c32")]),
    }, {"narratology": {
        "what_stands_out": [{"entity": "the rider", "against": "the snow",
                             "what_makes_it_stand_out": "colour",
                             "why_it_matters": "he is the agent"}],
        "strongest_cue": {"visible_cue": "the red coat"},
        "organizing_distinctions": [{"distinction": "agent against field",
                                     "side_a": "rider", "side_b": "snow"}],
        "overall_reading": "a rider in snow", "uncertainty": "none"}})
    return entry


class TestNormalization:
    def test_a_whole_acceptance_gives_both_roles_that_choice(self):
        entry = _entry()
        store.accept_proposal(entry, "1")
        origins = analysis.role_origins(entry)
        assert {r: origins[r]["choice"] for r in store.ROLES} == {
            store.ROLE_FIGURE: "1", store.ROLE_BACKGROUND: "1"}
        assert all(origins[r]["source"] == "proposal" for r in store.ROLES)

    def test_a_split_reports_each_role_separately(self):
        entry = _entry()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        store.set_role_from_proposal(entry, store.ROLE_BACKGROUND, "4")
        origins = analysis.role_origins(entry)
        assert origins[store.ROLE_FIGURE]["strategy"] == "direct"
        assert origins[store.ROLE_BACKGROUND]["strategy"] == "control"

    def test_a_hand_pick_is_distinguishable_from_a_generated_one(self):
        entry = _entry()
        store.accept_proposal(entry, "1")
        store.set_manual_colour(entry, store.ROLE_FIGURE,
                                {"rgb": [9, 9, 9], "hex": "#090909", "pipette": True})
        origins = analysis.role_origins(entry)
        assert origins[store.ROLE_FIGURE]["source"] == "manual"
        assert origins[store.ROLE_FIGURE]["pipette"] is True
        assert origins[store.ROLE_BACKGROUND]["source"] == "proposal"

    def test_every_role_carries_a_lab_value(self):
        entry = _entry()
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [9, 9, 9]})
        store.set_manual_colour(entry, store.ROLE_BACKGROUND, {"rgb": [8, 8, 8]})
        origins = analysis.role_origins(entry)
        assert all(len(origins[r]["lab"]) == 3 for r in store.ROLES)

    def test_the_narratological_reading_survives(self):
        view = analysis.narratology_view(_entry())
        assert view["stands_out"] == "the rider"
        assert view["against"] == "the snow"
        assert view["cue"] == "the red coat"

    def test_the_measurement_provenance_survives(self):
        view = analysis.proposal_view(_entry())
        figure = view["1"]["roles"][store.ROLE_FIGURE]
        assert figure["material"] == "the red coat"
        assert figure["segmentation_status"] == "success"


class TestCohorts:
    def test_boundaries_come_from_the_record_not_a_constant(self):
        entry = _entry()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        boundaries = analysis.feature_boundaries({"s": entry})
        assert boundaries["split_live"] is not None

    def test_a_frame_with_no_decision_is_not_miscohorted(self):
        assert analysis.cohort_of({}, {"split_live": None}) == analysis.COHORT_FULL


class TestDistances:
    def _row(self, entry, shot="s1"):
        return {
            "index": 0, "shot_id": shot, "scene": "1", "cohort": "full",
            "state": store.frame_state(entry),
            "roles": analysis.role_origins(entry),
            "proposals": analysis.proposal_view(entry),
        }

    def test_a_hand_pick_is_measured_against_every_alternative(self):
        entry = _entry()
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [198, 40, 30]})
        records = stats.manual_distances([self._row(entry)])
        assert len(records) == 1
        assert records[0]["nearest"]["choice"] == "1"
        assert records[0]["nearest"]["delta_e"] < 1.0

    def test_distance_does_not_depend_on_the_supersedes_link(self):
        entry = _entry()
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [198, 40, 30]})
        entry["manual"][store.ROLE_FIGURE].pop("supersedes", None)
        records = stats.manual_distances([self._row(entry)])
        assert records[0]["nearest"]["delta_e"] < 1.0

    def test_a_close_pick_is_not_called_a_failure(self):
        entry = _entry()
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [198, 40, 30]})
        row = self._row(entry)
        record = stats.manual_distances([row])[0]
        assert stats.classify_failure(record, row)["class"] == stats.FAILURE_NONE

    def test_a_frame_with_no_active_proposal_is_a_reduction_case(self):
        entry = _entry()
        for choice in entry["proposals"].values():
            choice["active"] = False
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [1, 2, 3]})
        row = self._row(entry)
        record = stats.manual_distances([row])[0]
        assert stats.classify_failure(record, row)["class"] == stats.FAILURE_REDUCTION


class TestCensus:
    def _rows(self, *entries):
        return [{
            "index": i, "shot_id": f"s{i}", "scene": "1", "cohort": "full",
            "state": store.frame_state(e), "review_answer": None,
            "generation_status": "ok",
            "roles": analysis.role_origins(e),
            "proposals": analysis.proposal_view(e),
        } for i, e in enumerate(entries)]

    def test_decision_shapes_separate_the_two_roles(self):
        whole = _entry(); store.accept_proposal(whole, "1")
        half = _entry()
        store.accept_proposal(half, "1")
        store.set_manual_colour(half, store.ROLE_FIGURE, {"rgb": [9, 9, 9]})
        both = _entry()
        store.set_manual_colour(both, store.ROLE_FIGURE, {"rgb": [9, 9, 9]})
        store.set_manual_colour(both, store.ROLE_BACKGROUND, {"rgb": [8, 8, 8]})
        result = stats.census(self._rows(whole, half, both))
        shapes = result["decision_shapes"]
        assert shapes["whole_proposal"] == 1
        assert shapes[f"manual_{store.ROLE_FIGURE}_only"] == 1
        assert shapes["fully_manual"] == 1

    def test_the_split_matrix_counts_role_pairs(self):
        entry = _entry()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        store.set_role_from_proposal(entry, store.ROLE_BACKGROUND, "4")
        matrix = stats.split_matrix(self._rows(entry))
        assert matrix["counts"]["P1"]["P4"] == 1
        assert matrix["both_from_same_proposal"] == 0


class TestReadOnly:
    def test_the_analysis_never_writes_to_the_review(self):
        import inspect

        from services import palette_review_report as report

        for module in (analysis, stats, report):
            source = inspect.getsource(module)
            assert "save_review" not in source, module.__name__
            assert "set_manual_colour" not in source, module.__name__
            assert "accept_proposal" not in source, module.__name__

    def test_output_stays_under_the_tests_tree(self):
        from services.palette_review_sheets import analysis_root

        root = analysis_root("/tmp/project", "tmdb_1")
        assert root.as_posix().endswith(
            "/tmp/project/outputs/tests/palette-review-analysis/tmdb_1")
