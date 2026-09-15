import numpy as np

from scripts.palette_lab import relational as E7


def _mask(size, x0, x1, y0, y1):
    mask = np.zeros((size, size), dtype=bool)
    mask[y0:y1, x0:x1] = True
    return mask


def _record(candidate_id, source, lab, mask, *, label="", role=None, hex_value="#000000"):
    return {
        "candidate_id": candidate_id,
        "review_id": candidate_id,
        "source": source,
        "label": label or candidate_id,
        "hex": hex_value,
        "rgb": [0, 0, 0],
        "lab": lab,
        "area_percent": round(float(mask.mean()) * 100, 3),
        "centroid": [0.5, 0.5],
        "semantic_role": role,
        "hierarchy_level": None if source == "semantic" else 3,
    }


def _figure_in_ground(size=40):
    """A compact figure fully surrounded by a large ground."""
    figure = _mask(size, 18, 22, 18, 22)
    ground = np.ones((size, size), dtype=bool)
    ground[10:30, 10:30] = False
    ground[12:28, 12:28] = True
    ground &= ~figure
    return figure, ground


class TestRing:
    def test_ring_is_the_shell_outside_the_mask(self):
        mask = _mask(20, 8, 12, 8, 12)
        shell = E7.ring(mask, 1)
        assert not (shell & mask).any()
        assert shell[7, 9] and shell[12, 9]
        assert not shell[6, 9]

    def test_ring_radius_grows_the_shell(self):
        mask = _mask(20, 8, 12, 8, 12)
        assert E7.ring(mask, 3).sum() > E7.ring(mask, 1).sum()


class TestDirectionality:
    def test_enclosure_is_asymmetric(self):
        figure, ground = _figure_in_ground()
        records = [
            _record("f", "semantic", [50.0, 40.0, 30.0], figure, role="chromatic"),
            _record("g", "semantic", [80.0, 1.0, 1.0], ground, role="atmospheric"),
        ]
        masks = {"f": figure, "g": ground}
        E7.lens_support(records, masks)
        geometry = E7.prepare_geometry(records, masks)
        forward = E7.pair_evidence(records[0], records[1], geometry, None, {})
        reverse = E7.pair_evidence(records[1], records[0], geometry, None, {})
        assert forward["spatial"]["enclosure"] > reverse["spatial"]["enclosure"]
        assert forward["spatial"]["relative_scale"] > reverse["spatial"]["relative_scale"]

    def test_symmetric_signals_stay_equal_under_swap(self):
        figure, ground = _figure_in_ground()
        records = [
            _record("f", "semantic", [50.0, 40.0, 30.0], figure, role="chromatic"),
            _record("g", "semantic", [80.0, 1.0, 1.0], ground, role="atmospheric"),
        ]
        masks = {"f": figure, "g": ground}
        E7.lens_support(records, masks)
        geometry = E7.prepare_geometry(records, masks)
        forward = E7.pair_evidence(records[0], records[1], geometry, None, {})
        reverse = E7.pair_evidence(records[1], records[0], geometry, None, {})
        assert forward["formal"]["delta_e"] == reverse["formal"]["delta_e"]
        assert forward["spatial"]["union_coverage"] == reverse["spatial"]["union_coverage"]
        assert forward["spatial"]["overlap_iou"] == reverse["spatial"]["overlap_iou"]


class TestFormal:
    def test_hue_relation_is_unavailable_for_near_neutral_colours(self):
        assert E7.hue_relation([50.0, 1.0, 1.0], [20.0, 40.0, 30.0])["available"] is False
        assert E7.hue_relation([50.0, 40.0, 0.0], [20.0, -40.0, 0.0])["available"] is True

    def test_delta_e_is_a_raw_distance(self):
        assert E7.delta_e([0.0, 0.0, 0.0], [0.0, 3.0, 4.0]) == 5.0


class TestDuplication:
    def test_colour_multiplicity_counts_identical_measured_colours(self):
        mask = _mask(20, 0, 4, 0, 4)
        records = [
            _record("a", "semantic", [50.0, 0.0, 0.0], mask, hex_value="#aabbcc"),
            _record("b", "semantic", [50.0, 0.0, 0.0], mask, hex_value="#aabbcc"),
            _record("c", "semantic", [10.0, 0.0, 0.0], mask, hex_value="#112233"),
        ]
        counts = E7.colour_multiplicity(records)
        assert counts["a"] == 2 and counts["b"] == 2 and counts["c"] == 1

    def test_multiplicity_is_never_a_scored_signal(self):
        scored = E7.FORMAL_SIGNALS + E7.SPATIAL_SIGNALS + E7.SEMANTIC_SIGNALS
        assert not any("multiplicity" in name for name in scored)

    def test_lens_support_measures_independent_agreement_not_duplication(self):
        figure = _mask(20, 4, 10, 4, 10)
        other = _mask(20, 4, 10, 4, 10)
        far = _mask(20, 14, 18, 14, 18)
        records = [
            _record("a", "semantic", [50.0, 0.0, 0.0], figure, role="narrative"),
            _record("b", "semantic", [50.0, 0.0, 0.0], other, role="chromatic"),
            _record("c", "semantic", [50.0, 0.0, 0.0], far, role="atmospheric"),
        ]
        masks = {"a": figure, "b": other, "c": far}
        support = E7.lens_support(records, masks)
        assert support["a"]["narrative"] == 1.0
        assert support["a"]["chromatic"] == 1.0
        assert "atmospheric" not in support["a"]
        assert sum(support["c"].values()) < sum(support["a"].values())

    def test_distinct_colour_pairs_collapses_repeats_and_keeps_the_best_rank(self):
        by_id = {
            "a": {"hex": "#111111"}, "a2": {"hex": "#111111"},
            "b": {"hex": "#222222"}, "c": {"hex": "#333333"},
        }
        ordered = [
            {"figure_id": "a", "ground_id": "b"},
            {"figure_id": "a2", "ground_id": "b"},
            {"figure_id": "c", "ground_id": "b"},
        ]
        collapsed = E7.distinct_colour_pairs(ordered, by_id, 10)
        assert len(collapsed) == 2
        assert collapsed[0]["rank"] == 1 and collapsed[0]["represents"] == 2
        assert collapsed[1]["rank"] == 3 and collapsed[1]["represents"] == 1


class TestNormalisation:
    def test_unavailable_signals_are_neutral_not_invented(self):
        assert E7.normalise([None, None]) == [0.5, 0.5]
        assert E7.normalise([0.0, None, 10.0]) == [0.0, 0.5, 1.0]

    def test_constant_signals_collapse_to_neutral(self):
        assert E7.normalise([3.0, 3.0, 3.0]) == [0.5, 0.5, 0.5]


class TestHypotheses:
    def _scored(self, weak_family_value):
        figure, ground = _figure_in_ground()
        records = [
            _record("f", "semantic", [50.0, 40.0, 30.0], figure, role="chromatic"),
            _record("g", "semantic", [80.0, 1.0, 1.0], ground, role="atmospheric"),
            _record("h", "hierarchy", [20.0, 2.0, 2.0], _mask(40, 0, 6, 0, 6)),
        ]
        masks = {"f": figure, "g": ground, "h": _mask(40, 0, 6, 0, 6)}
        return E7.build_pairs(records, masks, None, {"f": weak_family_value})

    def test_every_pair_reports_all_three_hypotheses(self):
        for pair in self._scored(0.5):
            assert set(pair["hypotheses"]) == set(E7.HYPOTHESES)
            assert set(pair["families"]) == {"formal", "spatial", "semantic"}

    def test_conjunctive_and_bottleneck_punish_a_single_weak_family(self):
        strong = {"formal": 0.9, "spatial": 0.9, "semantic": 0.9}
        lopsided = {"formal": 1.0, "spatial": 1.0, "semantic": 0.1}
        for name, values in (("strong", strong), ("lopsided", lopsided)):
            values["additive"] = sum(values[k] for k in ("formal", "spatial", "semantic")) / 3
            values["conjunctive"] = (values["formal"] * values["spatial"] * values["semantic"]) ** (1 / 3)
            values["bottleneck"] = min(values["formal"], values["spatial"], values["semantic"])
        assert lopsided["additive"] > strong["additive"] - 0.2
        assert lopsided["conjunctive"] < strong["conjunctive"]
        assert lopsided["bottleneck"] < strong["bottleneck"]

    def test_pairs_are_ordered_so_both_directions_are_evaluated(self):
        scored = self._scored(0.5)
        assert len(scored) == 6
        keys = {(pair["figure_id"], pair["ground_id"]) for pair in scored}
        assert ("f", "g") in keys and ("g", "f") in keys

    def test_ranking_is_deterministic(self):
        scored = self._scored(0.5)
        first = [
            (pair["figure_id"], pair["ground_id"])
            for pair in E7.rank_pairs(scored, "R2_conjunctive")
        ]
        second = [
            (pair["figure_id"], pair["ground_id"])
            for pair in E7.rank_pairs(list(reversed(scored)), "R2_conjunctive")
        ]
        assert first == second


class TestLocalContrast:
    def test_local_contrast_measures_the_neighbourhood_not_the_whole_image(self):
        size = 40
        figure, ground = _figure_in_ground(size)
        arr = np.zeros((size, size, 3), dtype=np.uint8)
        arr[...] = (10, 10, 10)
        arr[ground] = (250, 250, 250)
        records = [
            _record("f", "semantic", [10.0, 0.0, 0.0], figure, role="chromatic"),
            _record("g", "semantic", [98.0, 0.0, 0.0], ground, role="atmospheric"),
        ]
        masks = {"f": figure, "g": ground}
        E7.lens_support(records, masks)
        geometry = E7.prepare_geometry(records, masks)
        pair = E7.pair_evidence(records[0], records[1], geometry, arr, {})
        assert pair["formal"]["local_sample_pixels"] > 0
        assert pair["formal"]["local_delta_e"] > 50

    def test_local_contrast_is_none_without_an_image(self):
        figure, ground = _figure_in_ground()
        records = [
            _record("f", "semantic", [10.0, 0.0, 0.0], figure, role="chromatic"),
            _record("g", "semantic", [98.0, 0.0, 0.0], ground, role="atmospheric"),
        ]
        masks = {"f": figure, "g": ground}
        E7.lens_support(records, masks)
        geometry = E7.prepare_geometry(records, masks)
        pair = E7.pair_evidence(records[0], records[1], geometry, None, {})
        assert pair["formal"]["local_delta_e"] is None
