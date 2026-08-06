"""Tests for the atomic-write path in the shotlist/book write group.

`data/shotlist.py` (``write_shotlist()`` and the in-place rewrite inside
``migrate_shotlist_fields()``), `data/book.py` (``save_book()``), and the
per-book editing sidecars in `visualizers/book_visualizer.py`
(``_save_layers()``, ``_save_text_sels()``, ``_save_mask()``) all rewrote
their canonical files with a direct ``open(path, "w")``/``Path.write_text()``
call, so a crash mid-write could leave a truncated/corrupt file on disk. All
of them now go through ``data.annotate.atomic_write_text()``, which writes to
a same-directory temp file and swaps it in with ``os.replace()``.
"""

import json

import pytest

from data import annotate
from data.book import book_json_path, load_book, save_book
from data.shotlist import get_shotlist_path, migrate_shotlist_fields, read_shotlist, write_shotlist
from visualizers.book_visualizer import (
    _layers_path,
    _load_layers,
    _load_mask,
    _load_text_sels,
    _mask_path,
    _save_layers,
    _save_mask,
    _save_text_sels,
    _text_sel_path,
)


def _simulate_replace_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(annotate.os, "replace", _boom)


class TestWriteShotlist:
    SHOTS = [
        {"Ignore": "", "Scene": "1", "start_time": "00:00:00.000", "end_time": "00:00:01.000",
         "Shot_Caption": "cap1", "Scene_Caption": "scap1"},
        {"Ignore": "", "Scene": "1", "start_time": "00:00:01.000", "end_time": "00:00:02.000",
         "Shot_Caption": "cap2", "Scene_Caption": "scap1"},
    ]

    def test_round_trips_content(self, tmp_path):
        write_shotlist(str(tmp_path), "Film.mp4", "movie", [dict(s) for s in self.SHOTS])
        result = read_shotlist(str(tmp_path), "Film.mp4", "movie")
        assert [r["Shot_Caption"] for r in result] == ["cap1", "cap2"]
        assert [r["start_time"] for r in result] == ["00:00:00.000", "00:00:01.000"]

    def test_no_temp_file_left_behind(self, tmp_path):
        write_shotlist(str(tmp_path), "Film.mp4", "movie", [dict(s) for s in self.SHOTS])
        shotlist_path = get_shotlist_path(str(tmp_path), "Film.mp4", "movie")
        leftovers = [p for p in shotlist_path.parent.iterdir() if p != shotlist_path]
        assert leftovers == []

    def test_cleans_up_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        write_shotlist(str(tmp_path), "Film.mp4", "movie", [dict(s) for s in self.SHOTS])
        shotlist_path = get_shotlist_path(str(tmp_path), "Film.mp4", "movie")
        original = shotlist_path.read_text(encoding="utf-8")

        _simulate_replace_failure(monkeypatch)
        with pytest.raises(OSError):
            write_shotlist(str(tmp_path), "Film.mp4", "movie", [dict(self.SHOTS[0])])

        assert shotlist_path.read_text(encoding="utf-8") == original
        leftovers = [p for p in shotlist_path.parent.iterdir() if p != shotlist_path]
        assert leftovers == []


class TestMigrateShotlistFields:
    LEGACY_CSV = (
        "Ignore,Scene,Start,End,Start_Frame,End_Frame,Shot_Caption,Scene_Caption\r\n"
        ",1,00:00:00.000,00:00:01.000,0,30,cap1,scap1\r\n"
    )

    def _write_legacy_csv(self, tmp_path):
        shotlist_dir = tmp_path / "data" / "shotlists" / "movie"
        shotlist_dir.mkdir(parents=True)
        csv_path = shotlist_dir / "Film.csv"
        csv_path.write_text(self.LEGACY_CSV, encoding="utf-8")
        return csv_path

    def test_migrates_legacy_columns_atomically(self, tmp_path):
        csv_path = self._write_legacy_csv(tmp_path)

        results = migrate_shotlist_fields(str(tmp_path), media_type="movie")

        assert len(results) == 1
        assert results[0]["status"] == "migrated"
        assert results[0]["old_headers"] == ["Start", "End", "Start_Frame", "End_Frame"]

        migrated = read_shotlist(str(tmp_path), "Film.mp4", "movie")
        assert migrated == [{
            "Ignore": "", "Scene": "1", "start_time": "00:00:00.000", "end_time": "00:00:01.000",
            "start_frame": "0", "end_frame": "30", "shot_id": "",
            "Shot_Caption": "cap1", "Scene_Caption": "scap1",
        }]
        leftovers = [p for p in csv_path.parent.iterdir() if p != csv_path]
        assert leftovers == []

    def test_already_current_file_is_left_untouched(self, tmp_path):
        csv_path = self._write_legacy_csv(tmp_path)
        migrate_shotlist_fields(str(tmp_path), media_type="movie")
        before = csv_path.read_text(encoding="utf-8")

        results = migrate_shotlist_fields(str(tmp_path), media_type="movie")

        assert results[0]["status"] == "already_current"
        assert csv_path.read_text(encoding="utf-8") == before


class TestSaveBook:
    def test_round_trips_content(self, tmp_path):
        data = {"slug": "my-book", "pdf": None, "page_count": 3}
        save_book(str(tmp_path), "my-book", data)
        assert load_book(str(tmp_path), "my-book") == data

    def test_no_temp_file_left_behind(self, tmp_path):
        save_book(str(tmp_path), "my-book", {"slug": "my-book"})
        path = book_json_path(str(tmp_path), "my-book")
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []

    def test_cleans_up_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        path = book_json_path(str(tmp_path), "my-book")
        save_book(str(tmp_path), "my-book", {"slug": "my-book", "page_count": 1})
        original = path.read_text(encoding="utf-8")

        _simulate_replace_failure(monkeypatch)
        with pytest.raises(OSError):
            save_book(str(tmp_path), "my-book", {"slug": "my-book", "page_count": 2})

        assert path.read_text(encoding="utf-8") == original
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []


class TestBookVisualizerSidecars:
    def test_layers_round_trip_and_no_leftovers(self, tmp_path):
        layers = [{"id": "l1", "layer_subtype": "Engraving", "selected": True}]
        _save_layers(str(tmp_path), "my-book", layers)

        loaded = _load_layers(str(tmp_path), "my-book")
        assert loaded == [{"id": "l1", "layer_subtype": "Engraving", "line_weight": 1.0}]

        path = _layers_path(str(tmp_path), "my-book")
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []

    def test_layers_cleans_up_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        path = _layers_path(str(tmp_path), "my-book")
        _save_layers(str(tmp_path), "my-book", [{"id": "l1"}])
        original = path.read_text(encoding="utf-8")

        _simulate_replace_failure(monkeypatch)
        with pytest.raises(OSError):
            _save_layers(str(tmp_path), "my-book", [{"id": "l2"}])

        assert path.read_text(encoding="utf-8") == original
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []

    def test_text_sels_round_trip_and_no_leftovers(self, tmp_path):
        sels = [{"id": "s1", "text": "hello"}]
        _save_text_sels(str(tmp_path), "my-book", sels)

        assert _load_text_sels(str(tmp_path), "my-book") == sels
        path = _text_sel_path(str(tmp_path), "my-book")
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []

    def test_mask_round_trip_and_no_leftovers(self, tmp_path):
        _save_mask(str(tmp_path), "my-book", {3, 1, 2})

        assert _load_mask(str(tmp_path), "my-book") == {1, 2, 3}
        path = _mask_path(str(tmp_path), "my-book")
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []
