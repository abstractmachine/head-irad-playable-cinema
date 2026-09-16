import pytest

from data import palette_review as store
from services import palette_review as service


def _proposal(key, *, active=True, colours=None, label="DIRECT",
              strategy="direct", converges=None):
    return {
        "strategy": strategy,
        "label": label,
        "active": active,
        "materials": ["the red coat", "the snowy ground"],
        "converges_with": converges or [],
        "colours": colours if colours is not None else [
            {"rgb": [98, 13, 11], "hex": "#620d0b"},
            {"rgb": [140, 140, 147], "hex": "#8c8c93"},
        ],
    }


def _generated(entry=None, choices=("1", "2"), generation_id="gen-a"):
    entry = entry if entry is not None else {}
    proposals = {key: _proposal(key) for key in choices}
    return store.record_generation(
        entry, service.PROPOSAL_VERSION, generation_id, proposals,
        {"models": {"text": "qwen"}, "source_image": "frame.png"},
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A sandboxed project. Never touches the real prefs file."""
    values = {"path": str(tmp_path)}
    monkeypatch.setattr("tool.prefs.get", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr("tool.prefs.set", lambda key, value: values.__setitem__(key, value))
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

class TestGeneration:
    def test_generation_records_its_provenance(self):
        entry = _generated()
        generation = entry["proposal_generation"]
        assert generation["status"] == "ok"
        assert generation["version"] == service.PROPOSAL_VERSION
        assert generation["generation_id"] == "gen-a"
        assert generation["models"]["text"] == "qwen"

    def test_generation_never_implies_acceptance(self):
        entry = _generated()
        assert store.frame_state(entry) == store.STATE_UNREVIEWED
        assert "review" not in entry
        assert "final_palette" not in entry

    def test_a_failure_is_recorded_as_a_state(self):
        entry = store.record_generation_failure({}, service.PROPOSAL_VERSION,
                                                "gen-x", "SAM exploded")
        assert store.frame_state(entry) == store.STATE_GENERATION_FAILED
        assert "SAM exploded" in entry["proposal_generation"]["error"]

    def test_a_failure_does_not_destroy_earlier_proposals(self):
        entry = _generated()
        store.record_generation_failure(entry, service.PROPOSAL_VERSION,
                                        "gen-b", "boom")
        assert entry["proposals"]

    def test_running_generation_is_its_own_state(self):
        entry = store.begin_generation({}, service.PROPOSAL_VERSION, "gen-c")
        assert store.frame_state(entry) == store.STATE_GENERATING

    def test_partial_batch_failure_preserves_successful_frames(self, project):
        record = store.empty_record("f.mkv", "movie")
        _generated(store.frame(record, "shot-1"))
        store.record_generation_failure(store.frame(record, "shot-2"),
                                        service.PROPOSAL_VERSION, "g", "boom")
        _generated(store.frame(record, "shot-3"), generation_id="gen-c")
        store.save_review(project, "f.mkv", "movie", record)

        reloaded = store.load_review(project, "f.mkv", "movie")
        summary = store.media_summary(reloaded)
        assert summary[store.STATE_UNREVIEWED] == 2
        assert summary[store.STATE_GENERATION_FAILED] == 1

    def test_compatible_proposals_are_skipped_by_default(self):
        assert store.has_proposals(_generated()) is True

    def test_an_ungenerated_frame_has_no_proposals(self):
        assert store.has_proposals({}) is False
        assert store.frame_state({}) == store.STATE_NOT_GENERATED


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

class TestReview:
    def test_a_valid_choice_accepts_that_proposal(self):
        entry = store.accept_proposal(_generated(), "2")
        assert entry["review"]["answer"] == store.ANSWER_ACCEPTED
        assert entry["review"]["choice"] == "2"
        assert store.frame_state(entry) == store.STATE_ACCEPTED

    def test_acceptance_records_the_generation_it_was_looking_at(self):
        entry = store.accept_proposal(_generated(generation_id="gen-77"), "1")
        assert entry["review"]["accepted_generation_id"] == "gen-77"
        assert entry["review"]["accepted_generation_version"] == service.PROPOSAL_VERSION

    def test_acceptance_uses_the_stored_colours_without_recomputing(self):
        entry = store.accept_proposal(_generated(), "1")
        final = entry["final_palette"]
        assert final[store.ROLE_FIGURE]["hex"] == "#620d0b"
        assert final[store.ROLE_BACKGROUND]["hex"] == "#8c8c93"
        assert final["source"] == "proposal"

    def test_an_inactive_choice_cannot_be_accepted(self):
        entry = _generated()
        entry["proposals"]["2"] = _proposal("2", active=False)
        with pytest.raises(ValueError):
            store.accept_proposal(entry, "2")

    def test_an_unknown_choice_cannot_be_accepted(self):
        with pytest.raises(ValueError):
            store.accept_proposal(_generated(), "4")

    def test_a_half_measured_choice_cannot_be_accepted(self):
        entry = _generated()
        entry["proposals"]["1"] = _proposal(
            "1", colours=[{"rgb": [1, 2, 3], "hex": "#010203"}, None])
        with pytest.raises(ValueError):
            store.accept_proposal(entry, "1")

    def test_only_active_choices_are_selectable(self):
        entry = _generated()
        entry["proposals"]["2"] = _proposal("2", active=False, converges=["1"])
        assert store.selectable_choices(entry) == ["1"]

    def test_rejection_is_persisted(self):
        entry = store.reject_proposals(_generated(), reviewer="douglas")
        assert entry["review"]["answer"] == store.ANSWER_REJECTED
        assert entry["review"]["reviewer"] == "douglas"
        assert store.frame_state(entry) == store.STATE_REJECTED

    def test_rejection_differs_from_the_algorithmic_no_adequate_palette(self):
        entry = _generated()
        entry["proposals"]["3"] = _proposal(
            "3", strategy="no_adequate_two_colour",
            label="NO ADEQUATE TWO-COLOUR PALETTE", colours=[])
        store.reject_proposals(entry)
        assert entry["review"]["answer"] == store.ANSWER_REJECTED
        assert entry["proposals"]["3"]["strategy"] == "no_adequate_two_colour"
        assert store.ANSWER_REJECTED != "no_adequate_two_colour"

    def test_rejection_differs_from_unreviewed(self):
        assert store.frame_state(_generated()) == store.STATE_UNREVIEWED
        assert store.frame_state(store.reject_proposals(_generated())) == store.STATE_REJECTED

    def test_rejection_preserves_the_proposals(self):
        entry = store.reject_proposals(_generated())
        assert set(entry["proposals"]) == {"1", "2"}

    def test_rejection_clears_any_earlier_final_palette(self):
        entry = store.accept_proposal(_generated(), "1")
        store.reject_proposals(entry)
        assert "final_palette" not in entry


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_re_exposes_the_generated_proposals(self):
        entry = store.reset_frame(store.accept_proposal(_generated(), "1"))
        assert store.frame_state(entry) == store.STATE_UNREVIEWED
        assert store.selectable_choices(entry) == ["1", "2"]

    def test_generated_provenance_survives_reset(self):
        entry = store.reset_frame(store.accept_proposal(
            _generated(generation_id="gen-keep"), "1"))
        assert entry["proposal_generation"]["generation_id"] == "gen-keep"

    def test_reset_clears_the_final_palette_and_the_answer(self):
        entry = store.reset_frame(store.accept_proposal(_generated(), "1"))
        assert "final_palette" not in entry
        assert "review" not in entry

    def test_reset_also_clears_manual_work(self):
        entry = _generated()
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [1, 2, 3]})
        store.reset_frame(entry)
        assert store.frame_state(entry) == store.STATE_UNREVIEWED

    def test_generated_provenance_survives_a_manual_override(self):
        entry = _generated(generation_id="gen-m")
        store.reject_proposals(entry)
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [10, 20, 30]})
        store.set_manual_colour(entry, store.ROLE_BACKGROUND, {"rgb": [200, 200, 200]})
        assert entry["proposal_generation"]["generation_id"] == "gen-m"
        assert entry["proposals"]
        assert store.frame_state(entry) == store.STATE_MANUAL


# ---------------------------------------------------------------------------
# Manual authoring
# ---------------------------------------------------------------------------

class TestManual:
    def test_a_figure_colour_persists(self, project):
        record = store.empty_record("f.mkv", "movie")
        store.set_manual_colour(store.frame(record, "s1"), store.ROLE_FIGURE,
                                {"rgb": [200, 12, 10], "hex": "#c80c0a"})
        store.save_review(project, "f.mkv", "movie", record)
        reloaded = store.load_review(project, "f.mkv", "movie")
        manual = reloaded["frames"]["s1"]["manual"][store.ROLE_FIGURE]
        assert manual["rgb"] == [200, 12, 10]

    def test_a_background_colour_persists(self, project):
        record = store.empty_record("f.mkv", "movie")
        store.set_manual_colour(store.frame(record, "s1"), store.ROLE_BACKGROUND,
                                {"rgb": [8, 9, 10]})
        store.save_review(project, "f.mkv", "movie", record)
        reloaded = store.load_review(project, "f.mkv", "movie")
        assert reloaded["frames"]["s1"]["manual"][store.ROLE_BACKGROUND]["rgb"] == [8, 9, 10]

    def test_one_role_alone_is_an_incomplete_manual_palette(self):
        entry = store.set_manual_colour({}, store.ROLE_FIGURE, {"rgb": [1, 2, 3]})
        assert store.frame_state(entry) == store.STATE_MANUAL_INCOMPLETE
        assert "final_palette" not in entry

    def test_both_roles_complete_the_manual_palette(self):
        entry = store.set_manual_colour({}, store.ROLE_FIGURE, {"rgb": [1, 2, 3]})
        store.set_manual_colour(entry, store.ROLE_BACKGROUND, {"rgb": [4, 5, 6]})
        assert store.frame_state(entry) == store.STATE_MANUAL
        assert entry["final_palette"]["source"] == "manual"

    def test_a_pipetted_colour_is_not_replaced_by_a_mask_mean(self):
        entry = store.set_manual_colour(
            {}, store.ROLE_FIGURE,
            {"rgb": [220, 30, 25], "hex": "#dc1e19", "pipette": True},
            masks=[{"index": 0, "area": 900, "mask_mean_rgb": [98, 13, 11]}],
        )
        stored = entry["manual"][store.ROLE_FIGURE]
        assert stored["rgb"] == [220, 30, 25]
        assert stored["pipette"] is True

    def test_mask_provenance_survives_persistence(self, project):
        record = store.empty_record("f.mkv", "movie")
        store.set_manual_colour(
            store.frame(record, "s1"), store.ROLE_FIGURE, {"rgb": [1, 2, 3]},
            masks=[{"index": 2, "area": 512}, {"index": 5, "area": 64}],
        )
        store.save_review(project, "f.mkv", "movie", record)
        reloaded = store.load_review(project, "f.mkv", "movie")
        masks = reloaded["frames"]["s1"]["manual"][store.ROLE_FIGURE]["masks"]
        assert [item["index"] for item in masks] == [2, 5]

    def test_a_role_can_be_removed_without_resetting_the_frame(self):
        entry = store.set_manual_colour({}, store.ROLE_FIGURE, {"rgb": [1, 2, 3]})
        store.set_manual_colour(entry, store.ROLE_BACKGROUND, {"rgb": [4, 5, 6]})
        store.clear_manual_role(entry, store.ROLE_BACKGROUND)
        assert store.frame_state(entry) == store.STATE_MANUAL_INCOMPLETE

    def test_an_unknown_role_is_refused(self):
        with pytest.raises(ValueError):
            store.set_manual_colour({}, "middleground", {"rgb": [1, 2, 3]})

    def test_manual_work_outranks_an_earlier_acceptance(self):
        entry = store.accept_proposal(_generated(), "1")
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [1, 2, 3]})
        store.set_manual_colour(entry, store.ROLE_BACKGROUND, {"rgb": [4, 5, 6]})
        assert store.frame_state(entry) == store.STATE_MANUAL
        assert entry["final_palette"]["source"] == "manual"


# ---------------------------------------------------------------------------
# Media scope
# ---------------------------------------------------------------------------

class TestMediaScope:
    def test_clear_all_only_affects_the_selected_media_item(self, project):
        for filename in ("a.mkv", "b.mkv"):
            record = store.empty_record(filename, "movie")
            store.accept_proposal(_generated(store.frame(record, "s1")), "1")
            store.save_review(project, filename, "movie", record)

        service.clear_all(project, "a.mkv", "movie")

        assert store.frame_state(
            store.load_review(project, "a.mkv", "movie")["frames"]["s1"]
        ) == store.STATE_UNREVIEWED
        assert store.frame_state(
            store.load_review(project, "b.mkv", "movie")["frames"]["s1"]
        ) == store.STATE_ACCEPTED

    def test_movie_and_gameplay_stay_distinct(self, project):
        for media_type in ("movie", "gameplay"):
            record = store.empty_record("same.mkv", media_type)
            store.accept_proposal(_generated(store.frame(record, "s1")), "1")
            store.save_review(project, "same.mkv", media_type, record)

        service.clear_all(project, "same.mkv", "movie")

        assert store.frame_state(
            store.load_review(project, "same.mkv", "gameplay")["frames"]["s1"]
        ) == store.STATE_ACCEPTED

    def test_the_two_media_types_use_different_paths(self, project):
        movie = store.get_review_path(project, "same.mkv", "movie")
        gameplay = store.get_review_path(project, "same.mkv", "gameplay")
        assert movie != gameplay
        assert "movie" in str(movie) and "gameplay" in str(gameplay)

    def test_there_is_no_plural_media_directory(self, project):
        assert "/movies/" not in str(store.get_review_path(project, "a.mkv", "movie"))

    def test_clear_all_preserves_generated_proposals(self, project):
        record = store.empty_record("a.mkv", "movie")
        store.accept_proposal(_generated(store.frame(record, "s1")), "1")
        store.save_review(project, "a.mkv", "movie", record)

        service.clear_all(project, "a.mkv", "movie")

        entry = store.load_review(project, "a.mkv", "movie")["frames"]["s1"]
        assert entry["proposals"]
        assert entry["proposal_generation"]["status"] == "ok"

    def test_a_missing_record_loads_as_empty_not_an_error(self, project):
        record = store.load_review(project, "never-seen.mkv", "movie")
        assert record["frames"] == {}


# ---------------------------------------------------------------------------
# Service parity
# ---------------------------------------------------------------------------

class TestServiceParity:
    def test_every_persistent_mutation_is_exposed_by_the_service(self):
        for name in ("create_frame", "create_all", "accept", "reject", "reset",
                     "set_manual", "clear_manual", "clear_all", "media_state",
                     "list_frames"):
            assert callable(getattr(service, name)), name

    def test_the_service_round_trips_accept_through_disk(self, project):
        record = store.empty_record("a.mkv", "movie")
        _generated(store.frame(record, "s1"))
        store.save_review(project, "a.mkv", "movie", record)

        service.accept(project, "a.mkv", "movie", "s1", "2", reviewer="cli")

        entry = store.load_review(project, "a.mkv", "movie")["frames"]["s1"]
        assert entry["review"]["choice"] == "2"
        assert entry["review"]["reviewer"] == "cli"

    def test_the_service_round_trips_reject_and_reset(self, project):
        record = store.empty_record("a.mkv", "movie")
        _generated(store.frame(record, "s1"))
        store.save_review(project, "a.mkv", "movie", record)

        service.reject(project, "a.mkv", "movie", "s1")
        assert store.frame_state(
            store.load_review(project, "a.mkv", "movie")["frames"]["s1"]
        ) == store.STATE_REJECTED

        service.reset(project, "a.mkv", "movie", "s1")
        assert store.frame_state(
            store.load_review(project, "a.mkv", "movie")["frames"]["s1"]
        ) == store.STATE_UNREVIEWED

    def test_the_service_round_trips_manual_authoring(self, project):
        service.set_manual(project, "a.mkv", "movie", "s1", store.ROLE_FIGURE,
                           {"rgb": [9, 9, 9], "hex": "#090909", "pipette": True})
        entry = store.load_review(project, "a.mkv", "movie")["frames"]["s1"]
        assert entry["manual"][store.ROLE_FIGURE]["pipette"] is True
        assert store.frame_state(entry) == store.STATE_MANUAL_INCOMPLETE

    def test_no_persistent_mutation_lives_only_in_qt_code(self):
        import inspect

        from visualizers import palette_visualizer

        source = inspect.getsource(palette_visualizer)
        assert "save_review(" not in source
        assert "atomic_write_text" not in source


# ---------------------------------------------------------------------------
# Derived state
# ---------------------------------------------------------------------------

class TestSplitValidation:
    """Figure from one proposal, background from another."""

    def _two(self):
        entry = {}
        store.record_generation(entry, service.PROPOSAL_VERSION, "gen-a", {
            "1": _proposal("1", label="DIRECT", strategy="direct", colours=[
                {"rgb": [1, 1, 1], "hex": "#010101"},
                {"rgb": [2, 2, 2], "hex": "#020202"}]),
            "4": _proposal("4", label="CONTROL", strategy="control", colours=[
                {"rgb": [3, 3, 3], "hex": "#030303"},
                {"rgb": [4, 4, 4], "hex": "#040404"}]),
        }, {})
        return entry

    def test_each_half_comes_from_its_own_proposal(self):
        entry = self._two()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        store.set_role_from_proposal(entry, store.ROLE_BACKGROUND, "4")
        final = entry["final_palette"]
        assert final["source"] == "split"
        assert final[store.ROLE_FIGURE]["hex"] == "#010101"
        assert final[store.ROLE_BACKGROUND]["hex"] == "#040404"
        assert store.frame_state(entry) == store.STATE_SPLIT

    def test_the_record_says_which_strategy_won_each_half(self):
        entry = self._two()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        store.set_role_from_proposal(entry, store.ROLE_BACKGROUND, "4")
        review = entry["review"]
        assert review["answer"] == store.ANSWER_SPLIT
        assert review["choice"] is None
        assert review["roles"][store.ROLE_FIGURE]["choice"] == "1"
        assert review["roles"][store.ROLE_FIGURE]["strategy"] == "direct"
        assert review["roles"][store.ROLE_BACKGROUND]["choice"] == "4"
        assert review["roles"][store.ROLE_BACKGROUND]["strategy"] == "control"

    def test_the_colours_are_never_copied_into_the_review(self):
        entry = self._two()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        endorsement = entry["review"]["roles"][store.ROLE_FIGURE]
        assert "rgb" not in endorsement and "hex" not in endorsement

    def test_one_half_alone_is_incomplete_but_still_shows(self):
        entry = self._two()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        assert store.frame_state(entry) == store.STATE_SPLIT_INCOMPLETE
        assert "final_palette" not in entry
        resolved = store.resolved_roles(entry)
        assert resolved[store.ROLE_FIGURE]["hex"] == "#010101"
        assert store.ROLE_BACKGROUND not in resolved

    def test_both_halves_on_one_proposal_is_a_plain_acceptance(self):
        entry = self._two()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "4")
        store.set_role_from_proposal(entry, store.ROLE_BACKGROUND, "4")
        assert store.frame_state(entry) == store.STATE_ACCEPTED
        assert entry["review"]["answer"] == store.ANSWER_ACCEPTED
        assert entry["review"]["choice"] == "4"
        assert "roles" not in entry["review"]

    def test_taking_one_half_decomposes_a_whole_acceptance(self):
        entry = self._two()
        store.accept_proposal(entry, "4")
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        assert store.frame_state(entry) == store.STATE_SPLIT
        # The untouched half stays where the reviewer already put it.
        assert entry["review"]["roles"][store.ROLE_BACKGROUND]["choice"] == "4"
        assert entry["final_palette"][store.ROLE_BACKGROUND]["hex"] == "#040404"
        assert entry["final_palette"][store.ROLE_FIGURE]["hex"] == "#010101"

    def test_a_pipette_replaces_only_its_own_half(self):
        entry = self._two()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        store.set_role_from_proposal(entry, store.ROLE_BACKGROUND, "4")
        store.set_manual_colour(entry, store.ROLE_FIGURE,
                                {"rgb": [9, 9, 9], "hex": "#090909", "pipette": True})
        assert store.frame_state(entry) == store.STATE_SPLIT
        final = entry["final_palette"]
        assert final[store.ROLE_FIGURE]["hex"] == "#090909"
        assert final[store.ROLE_BACKGROUND]["choice"] == "4"

    def test_a_pipette_drops_the_endorsement_it_supersedes(self):
        entry = self._two()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        store.set_role_from_proposal(entry, store.ROLE_BACKGROUND, "4")
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [9, 9, 9]})
        # A stale endorsement would claim proposal 1 was validated when it was not.
        assert store.ROLE_FIGURE not in entry["review"]["roles"]

    def test_pipetting_the_last_endorsed_half_leaves_no_review(self):
        entry = self._two()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [9, 9, 9]})
        assert "review" not in entry
        assert store.frame_state(entry) == store.STATE_MANUAL_INCOMPLETE

    def test_taking_a_half_replaces_a_pipetted_half(self):
        entry = self._two()
        store.set_manual_colour(entry, store.ROLE_FIGURE, {"rgb": [9, 9, 9]})
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        assert (entry.get("manual") or {}).get(store.ROLE_FIGURE) is None
        assert store.resolved_roles(entry)[store.ROLE_FIGURE]["hex"] == "#010101"

    def test_a_half_from_an_unselectable_proposal_is_refused(self):
        entry = self._two()
        entry["proposals"]["3"] = _proposal("3", active=True, colours=[])
        with pytest.raises(ValueError):
            store.set_role_from_proposal(entry, store.ROLE_FIGURE, "3")
        with pytest.raises(ValueError):
            store.set_role_from_proposal(entry, "middle", "1")

    def test_accepting_a_whole_proposal_clears_the_split(self):
        entry = self._two()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        store.set_role_from_proposal(entry, store.ROLE_BACKGROUND, "4")
        store.accept_proposal(entry, "1")
        assert entry["review"]["answer"] == store.ANSWER_ACCEPTED
        assert "roles" not in entry["review"]
        assert entry["final_palette"]["source"] == "proposal"

    def test_reset_returns_a_split_frame_to_its_proposals(self):
        entry = self._two()
        store.set_role_from_proposal(entry, store.ROLE_FIGURE, "1")
        store.set_role_from_proposal(entry, store.ROLE_BACKGROUND, "4")
        store.reset_frame(entry)
        assert store.frame_state(entry) == store.STATE_UNREVIEWED
        assert set(entry["proposals"]) == {"1", "4"}

    def test_the_service_persists_a_split(self, project):
        record = store.empty_record("a.mkv", "movie")
        store.frame(record, "s1").update(self._two())
        store.save_review(project, "a.mkv", "movie", record)

        service.set_role_choice(project, "a.mkv", "movie", "s1",
                                store.ROLE_FIGURE, "1")
        service.set_role_choice(project, "a.mkv", "movie", "s1",
                                store.ROLE_BACKGROUND, "4")
        entry = store.load_review(project, "a.mkv", "movie")["frames"]["s1"]
        assert store.frame_state(entry) == store.STATE_SPLIT
        assert entry["final_palette"][store.ROLE_FIGURE]["hex"] == "#010101"
        assert entry["final_palette"][store.ROLE_BACKGROUND]["hex"] == "#040404"


class TestDerivedState:
    def test_every_documented_state_is_reachable(self):
        reachable = {
            store.frame_state({}),
            store.frame_state(store.begin_generation({}, "v", "g")),
            store.frame_state(store.record_generation_failure({}, "v", "g", "e")),
            store.frame_state(_generated()),
            store.frame_state(store.accept_proposal(_generated(), "1")),
            store.frame_state(store.reject_proposals(_generated())),
            store.frame_state(store.set_manual_colour({}, store.ROLE_FIGURE, {"rgb": [1, 1, 1]})),
        }
        entry = store.set_manual_colour({}, store.ROLE_FIGURE, {"rgb": [1, 1, 1]})
        reachable.add(store.frame_state(
            store.set_manual_colour(entry, store.ROLE_BACKGROUND, {"rgb": [2, 2, 2]})))
        half = store.set_role_from_proposal(_generated(), store.ROLE_FIGURE, "1")
        reachable.add(store.frame_state(half))
        reachable.add(store.frame_state(
            store.set_role_from_proposal(half, store.ROLE_BACKGROUND, "2")))
        assert reachable == set(store.STATES)

    def test_the_media_summary_counts_by_state(self):
        record = store.empty_record("a.mkv", "movie")
        _generated(store.frame(record, "s1"))
        store.accept_proposal(_generated(store.frame(record, "s2")), "1")
        store.reject_proposals(_generated(store.frame(record, "s3")))
        summary = store.media_summary(record)
        assert summary[store.STATE_UNREVIEWED] == 1
        assert summary[store.STATE_ACCEPTED] == 1
        assert summary[store.STATE_REJECTED] == 1
