"""Tests for the atomic-write path in Tier 2 canonical project data.

`data/motif.py` (the annotation-JSON rewrite inside
``generate_motifs_for_movie()``), `data/film_motif.py` (``set_film_title()``
and ``generate_film_title()``, both writing the per-film title JSON), and
`data/index.py` (``_save_fields_yaml()``, backing ``save_fields()`` /
``save_atomic_fields()`` / ``save_vocabulary_fields()``) all used to call
``Path.write_text()`` / ``open(path, "w")`` directly, so a crash mid-write
could leave the file truncated or corrupt. All of them now go through
``data.annotate.atomic_write_text()``, which writes to a same-directory temp
file and swaps it in with ``os.replace()``.
"""

import json
from pathlib import Path

import pytest

from data import annotate
from data import motif as motif_mod
from data.annotate import get_annotation_json_path
from data.film_motif import get_film_title_path, load_film_motif, set_film_title
from data.index import load_atomic_fields, load_fields, save_atomic_fields, save_fields


def _fields_path(project_path) -> Path:
    return Path(project_path) / "preferences" / "data" / "fields.yaml"


def _simulate_replace_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(annotate.os, "replace", _boom)


def _make_annotation_json(tmp_path, entries):
    path = get_annotation_json_path(str(tmp_path), "Film.mp4", "movie")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


class TestGenerateMotifsForMovieWrite:
    def test_writes_motif_atomically_and_no_leftovers(self, tmp_path, monkeypatch):
        entries = [{"shot": {"shot_id": "a@f000000-f000030", "annotation": {}}}]
        json_path = _make_annotation_json(tmp_path, entries)

        monkeypatch.setattr(motif_mod, "generate_motif", lambda *a, **k: "Crossing.")

        result = motif_mod.generate_motifs_for_movie(
            str(tmp_path), "Film.mp4", media_type="movie", pipeline=object(),
        )

        assert result["processed"] == 1
        saved = json.loads(json_path.read_text(encoding="utf-8"))
        assert saved[0]["shot"]["motif"] == "crossing"

        leftovers = [p for p in json_path.parent.iterdir() if p != json_path]
        assert leftovers == []

    def test_cleans_up_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        entries = [{"shot": {"shot_id": "a@f000000-f000030", "annotation": {}}}]
        json_path = _make_annotation_json(tmp_path, entries)
        original = json_path.read_text(encoding="utf-8")

        monkeypatch.setattr(motif_mod, "generate_motif", lambda *a, **k: "crossing")
        _simulate_replace_failure(monkeypatch)

        with pytest.raises(OSError):
            motif_mod.generate_motifs_for_movie(
                str(tmp_path), "Film.mp4", media_type="movie", pipeline=object(),
            )

        assert json_path.read_text(encoding="utf-8") == original
        leftovers = [p for p in json_path.parent.iterdir() if p != json_path]
        assert leftovers == []


class TestSetFilmTitle:
    def test_round_trip(self, tmp_path):
        result = set_film_title(str(tmp_path), "Film.mp4", "movie", "carrying")
        assert result["value"] == "carrying"

        loaded = load_film_motif(str(tmp_path), "Film.mp4", "movie")
        assert loaded["value"] == "carrying"

    def test_overwrite_replaces_previous_value(self, tmp_path):
        set_film_title(str(tmp_path), "Film.mp4", "movie", "carrying")
        set_film_title(str(tmp_path), "Film.mp4", "movie", "crossing")

        loaded = load_film_motif(str(tmp_path), "Film.mp4", "movie")
        assert loaded["value"] == "crossing"

    def test_no_temp_file_left_behind(self, tmp_path):
        set_film_title(str(tmp_path), "Film.mp4", "movie", "carrying")
        path = get_film_title_path(str(tmp_path), "Film.mp4", "movie")
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []

    def test_cleans_up_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        set_film_title(str(tmp_path), "Film.mp4", "movie", "carrying")
        path = get_film_title_path(str(tmp_path), "Film.mp4", "movie")
        original = path.read_text(encoding="utf-8")

        _simulate_replace_failure(monkeypatch)
        with pytest.raises(OSError):
            set_film_title(str(tmp_path), "Film.mp4", "movie", "crossing")

        assert path.read_text(encoding="utf-8") == original
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []


class TestSaveFieldsYaml:
    def test_round_trip(self, tmp_path):
        save_fields(str(tmp_path), ["title", "year", "description"])
        assert load_fields(str(tmp_path)) == ["title", "year", "description"]

    def test_preserves_other_keys(self, tmp_path):
        save_fields(str(tmp_path), ["title", "year"])
        save_atomic_fields(str(tmp_path), ["camera", "shot"])

        assert load_fields(str(tmp_path)) == ["title", "year"]
        assert load_atomic_fields(str(tmp_path)) == ["camera", "shot"]

    def test_no_temp_file_left_behind(self, tmp_path):
        save_fields(str(tmp_path), ["title"])
        path = _fields_path(str(tmp_path))
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []

    def test_cleans_up_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        save_fields(str(tmp_path), ["title"])
        path = _fields_path(str(tmp_path))
        original = path.read_text(encoding="utf-8")

        _simulate_replace_failure(monkeypatch)
        with pytest.raises(OSError):
            save_fields(str(tmp_path), ["title", "year"])

        assert path.read_text(encoding="utf-8") == original
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []
