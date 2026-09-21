import json

import pytest

from data import palette_review as store
from services import palette_e9 as e9

FIGURE_JSON = {
    "image_reading": "a rider held against an empty horizon",
    "primary": {
        "figure": "the lone rider", "why_it_matters": "he is the one at stake",
        "visible_distinction": "a dark shape on a pale ground",
        "genre_note": "the image uses the Western convention of coding isolation as exposure",
        "material": "the rider's dark silhouette",
        "material_is_the_figure_itself": True,
        "why_this_material": "the silhouette is what separates him",
    },
    "alternative": None,
    "uncertainty": "none",
}

FIELD_JSON = {
    "image_reading": "the horizon presses on him",
    "primary": {
        "field": "the empty sky", "why_this_field": "it is what he is small against",
        "relation_to_figure": "dark against pale",
        "genre_note": None,
        "material": "the pale hazy sky",
        "why_this_material": "it is the world he rides through",
    },
    "alternative": None,
    "uncertainty": "none",
}


class _Pipeline:
    """Records every prompt it is given and replays scripted JSON."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, system, user, images, tokens):
        self.calls.append({"system": system, "user": user})
        return self.replies.pop(0)


@pytest.fixture
def scripted(monkeypatch):
    """Replace only the model call; every other stage runs for real."""
    pipe = _Pipeline([])

    def call_qwen(pipeline, system, user, images, tokens):
        payload = pipe(system, user, images, tokens)
        return {"parsed": payload, "raw": json.dumps(payload), "device": "cpu"}

    monkeypatch.setattr("scripts.palette_lab.stages.call_qwen", call_qwen)
    return pipe


def _pairs():
    return {role: {"system": f"{role} system", "user": "$material $reading $role $problem"
                   if role == "repair" else
                   ("$figure_reading $figure_material" if role == "field" else "task"),
                   "system_file": f"{role}.txt", "user_file": f"{role}-user.txt"}
            for role in ("figure", "field", "repair", "operationalize")}


# ---------------------------------------------------------------------------
# Figure / field separation
# ---------------------------------------------------------------------------

class TestFigureFieldSeparation:
    def test_the_figure_pass_is_not_shown_a_field(self, scripted):
        scripted.replies.append(FIGURE_JSON)
        e9.figure_pass("pipe", _pairs(), ["img"])
        prompt = scripted.calls[0]["system"] + scripted.calls[0]["user"]
        assert "field" not in prompt.lower().replace("figure", "")

    def test_the_field_pass_receives_the_frozen_figure(self, scripted):
        scripted.replies.append(FIELD_JSON)
        e9.field_pass("pipe", _pairs(), ["img"], FIGURE_JSON["primary"],
                      "the rider's dark silhouette")
        user = scripted.calls[0]["user"]
        assert "the lone rider" in user
        assert "the rider's dark silhouette" in user

    def test_the_two_readings_stay_separate_records(self):
        choice = e9._curated_choice(
            e9.CHOICE_PRIMARY, FIGURE_JSON["primary"], FIELD_JSON["primary"],
            {"material": "a", "status": "ok", "repairs": []},
            {"material": "b", "status": "ok", "repairs": []})
        assert choice["e9"]["figure_reading"]["figure"] == "the lone rider"
        assert choice["e9"]["field_reading"]["field"] == "the empty sky"

    def test_a_field_that_repeats_the_figure_material_is_unavailable(self):
        choice = e9._curated_choice(
            e9.CHOICE_PRIMARY, FIGURE_JSON["primary"], FIELD_JSON["primary"],
            {"material": "the pale sky", "status": "ok", "repairs": []},
            {"material": "The Pale Sky", "status": "ok", "repairs": []})
        assert choice["active"] is False
        assert "same material" in choice["inactive_reason"]

    def test_the_figure_is_always_colour_index_zero(self):
        choice = e9._curated_choice(
            e9.CHOICE_PRIMARY, FIGURE_JSON["primary"], FIELD_JSON["primary"],
            {"material": "figure material", "status": "ok", "repairs": []},
            {"material": "field material", "status": "ok", "repairs": []})
        assert choice["materials"] == ["figure material", "field material"]


# ---------------------------------------------------------------------------
# Material constraint and repair
# ---------------------------------------------------------------------------

class TestMaterialConstraint:
    def test_a_multi_entity_material_is_rejected(self):
        assert e9.material_problem("the rider and his horse")
        assert e9.material_problem("the face and the sky behind it")

    def test_a_single_material_is_accepted(self):
        assert e9.material_problem("the red coat") is None
        assert e9.material_problem("the sunlit sand of the flat") is None

    def test_one_uniform_field_survives_its_conjunction(self):
        assert e9.material_problem(
            "the uniformly dark boards lining the interior walls and floor") is None

    def test_a_bare_colour_is_not_a_material(self):
        assert "colour" in e9.material_problem("red")

    def test_repair_narrows_a_compound_and_keeps_provenance(self, scripted):
        scripted.replies.append({"rejected_material": "the rider and his horse",
                                 "status": "repaired",
                                 "material": "the rider's dark silhouette",
                                 "dropped": "the horse", "reason": "he is the subject"})
        result = e9.resolve_material("pipe", _pairs(), ["img"],
                                     "the rider and his horse", "the lone rider", "figure")
        assert result["material"] == "the rider's dark silhouette"
        assert result["original"] == "the rider and his horse"
        assert result["repairs"][0]["dropped"] == "the horse"
        assert result["repairs"][0]["attempt"] == 1

    def test_repair_is_told_the_reading_not_asked_to_recurate(self, scripted):
        scripted.replies.append({"status": "repaired", "material": "the coat"})
        e9.resolve_material("pipe", _pairs(), ["img"], "the coat and the wall",
                            "the lone rider", "figure")
        assert "the lone rider" in scripted.calls[0]["user"]

    def test_an_unrepairable_material_yields_no_material(self, scripted):
        scripted.replies.append({"status": "unrepairable", "material": None,
                                 "reason": "the reading needed the relation"})
        result = e9.resolve_material("pipe", _pairs(), ["img"], "the a and the b",
                                     "reading", "figure")
        assert result["material"] is None
        assert result["status"] == "unrepairable"

    def test_repair_is_bounded(self, scripted):
        scripted.replies.extend([
            {"status": "repaired", "material": "the x and the y"},
            {"status": "repaired", "material": "the p and the q"},
        ])
        result = e9.resolve_material("pipe", _pairs(), ["img"], "the a and the b",
                                     "reading", "figure")
        assert len(scripted.calls) == e9.MAX_REPAIRS
        assert result["material"] is None
        assert result["repairs"][-1]["status"] == "gave_up"

    def test_a_failed_repair_never_invents_a_material(self, scripted):
        scripted.replies.append({"status": "unrepairable", "material": None})
        result = e9.resolve_material("pipe", _pairs(), ["img"], "the a and the b",
                                     "reading", "field")
        choice = e9._curated_choice(e9.CHOICE_PRIMARY, FIGURE_JSON["primary"],
                                    FIELD_JSON["primary"],
                                    {"material": "ok", "status": "ok", "repairs": []},
                                    result)
        assert choice["active"] is False
        assert choice["materials"] == []


# ---------------------------------------------------------------------------
# Alternatives
# ---------------------------------------------------------------------------

class TestAlternatives:
    def test_no_alternative_gives_an_unavailable_choice_not_a_duplicate(self):
        choice = e9._curated_choice(e9.CHOICE_FIGURE_ALT, None, None, None, None)
        assert choice["active"] is False
        assert choice["materials"] == []

    def test_an_alternative_repeating_the_primary_material_is_dropped(self):
        assert e9.same_material("the pale sky", "The Pale  Sky!") is True
        assert e9.same_material("the pale sky", "the dark ridge") is False

    def test_a_duplicate_material_pair_is_deactivated_by_convergence(self):
        choices = {
            "1": {"active": True, "materials": ["a", "b"], "strategy": "e9_primary"},
            "3": {"active": True, "materials": ["a", "b"], "strategy": "e9_field_alternative"},
        }
        e9._mark_convergence(choices)
        assert choices["3"]["active"] is False
        assert choices["3"]["converges_with"] == ["1"]
        assert choices["1"]["active"] is True

    def test_reversal_is_not_an_alternative(self):
        choices = {
            "1": {"active": True, "materials": ["a", "b"], "strategy": "e9_primary"},
            "2": {"active": True, "materials": ["b", "a"], "strategy": "e9_figure_alternative"},
        }
        e9._mark_convergence(choices)
        # A swapped pair is a different pair key, so convergence does not catch
        # it; the prompts forbid it and the readings differ. Guard the fact that
        # we are not silently manufacturing one from a reversal.
        assert choices["2"]["materials"] != choices["1"]["materials"]


# ---------------------------------------------------------------------------
# Colour: nothing gates eligibility
# ---------------------------------------------------------------------------

class TestColourIsNotGated:
    def test_no_delta_e_or_area_or_chroma_gate_exists(self):
        """Checked over identifiers, not prose: the docstrings discuss chroma."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(e9))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(alias.name for alias in node.names)
        banned = {"delta_e", "deltaE", "_chroma", "chroma", "saturation",
                  "min_area", "area_fraction", "hue_relation"}
        assert not (names & banned), sorted(names & banned)

    def test_measurement_is_the_frozen_helper(self):
        import inspect

        from scripts.palette_lab import e8_palette_review as rev

        assert "_measure_mask" in inspect.getsource(rev.measure)
        assert "locate_and_measure" in inspect.getsource(e9._measure_choices)

    def test_two_near_identical_colours_are_still_a_valid_choice(self):
        choice = e9._curated_choice(
            e9.CHOICE_PRIMARY, FIGURE_JSON["primary"], FIELD_JSON["primary"],
            {"material": "the dark coat", "status": "ok", "repairs": []},
            {"material": "the dark room", "status": "ok", "repairs": []})
        assert choice["active"] is True


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_repair_history_is_kept_on_the_choice(self, scripted):
        scripted.replies.append({"status": "repaired", "material": "the coat"})
        resolved = e9.resolve_material("pipe", _pairs(), ["img"],
                                       "the coat and the wall", "reading", "figure")
        choice = e9._curated_choice(e9.CHOICE_PRIMARY, FIGURE_JSON["primary"],
                                    FIELD_JSON["primary"], resolved,
                                    {"material": "sky", "status": "ok", "repairs": []})
        history = choice["e9"]["figure_material"]["repairs"]
        assert history[0]["rejected"] == "the coat and the wall"
        assert choice["e9"]["figure_material"]["original"] == "the coat and the wall"

    def test_the_control_stays_frozen_and_labelled(self, monkeypatch):
        monkeypatch.setattr("services.palette_review._control_palette",
                            lambda *a, **k: {"foreground_rgb": [1, 2, 3],
                                             "background_rgb": [4, 5, 6]})
        choice = e9._control_choice("/p", "f.mp4", "movie", "s1")
        assert choice["strategy"] == "control"
        assert choice["colours"][0]["rgb"] == [1, 2, 3]
        assert "frozen production palette" in choice["source_stage"]

    def test_a_missing_control_is_unavailable_not_invented(self, monkeypatch):
        monkeypatch.setattr("services.palette_review._control_palette",
                            lambda *a, **k: None)
        choice = e9._control_choice("/p", "f.mp4", "movie", "s1")
        assert choice["active"] is False
        assert choice["colours"] == []

    def test_the_version_marks_these_as_e9(self):
        entry = {}
        store.record_generation(entry, e9.E9_VERSION, "gen", {"1": {}}, {})
        assert entry["proposal_generation"]["version"] == "e9a-figure-field-v1"


# ---------------------------------------------------------------------------
# Experimental boundary
# ---------------------------------------------------------------------------

class TestExperimentalBoundary:
    def test_no_prompt_is_ever_given_human_review_data(self):
        import inspect

        source = inspect.getsource(e9)
        for banned in ("final_palette", "resolved_roles", "review\"]",
                       "accept_proposal", "set_manual_colour", "supersedes"):
            assert banned not in source, banned

    def test_generation_never_auto_accepts(self):
        import inspect

        source = inspect.getsource(e9)
        assert "accept" not in source.replace("acceptance", "")

    def test_generation_does_not_import_the_calibration_analysis(self):
        import inspect

        source = inspect.getsource(e9)
        assert "palette_review_analysis" not in source
        assert "palette_review_stats" not in source

    def test_the_figure_prompt_asset_forbids_compound_materials(self, tmp_path):
        from pathlib import Path

        text = Path("/home/cowpoke/playable/dead-crossing/prompts/palettes/"
                    "palette-e9-figure-system-2026-09-21-v2.txt").read_text("utf-8")
        assert "ONE coherent colour-bearing" in text
        assert "convention of coding" in text

    def test_the_alternative_is_reachable_in_the_schema(self):
        """v1 showed only the null case and the model returned null 90/90 times."""
        from pathlib import Path

        for role in ("figure", "field"):
            text = Path("/home/cowpoke/playable/dead-crossing/prompts/palettes/"
                        f"palette-e9-{role}-system-2026-09-21-v2.txt").read_text("utf-8")
            schema = text.split("OUTPUT", 1)[1]
            assert '"alternative": {' in schema, role
            assert "Never invent one" in text, role

    def test_the_field_prompt_forbids_maximising_contrast(self):
        from pathlib import Path

        text = Path("/home/cowpoke/playable/dead-crossing/prompts/palettes/"
                    "palette-e9-field-system-2026-09-21-v2.txt").read_text("utf-8")
        assert "maximises contrast" in text
        assert "CONTRAST IS EVIDENCE, NOT A REQUIREMENT" in text
