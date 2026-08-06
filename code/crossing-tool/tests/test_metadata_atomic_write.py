"""Tests for the atomic-write path in the canonical JSON metadata store.

`data/metadata.py`'s ``save_json_metadata()`` rewrites the canonical
``<project>/data/metadata/<media_type>.json`` file — the single write site
used by ``set_metadata()``, ``prune_metadata()``, ``upsert_json_record()``,
and ``migrate_csv_to_json()``. It used to call ``Path.write_text()``
directly, so a crash mid-write could leave the file truncated or corrupt.
It now goes through ``data.annotate.atomic_write_text()``, which writes to a
same-directory temp file and swaps it in with ``os.replace()``.
"""

import json

import pytest

from data import annotate
from data.metadata import _json_path, load_json_metadata, save_json_metadata, upsert_json_record


class TestSaveJsonMetadata:
    def test_successful_write_round_trips_content(self, tmp_path):
        records = [{"media_id": "tmdb_1", "filename": "Film.mp4", "title": "Film"}]
        save_json_metadata(str(tmp_path), "movie", records)

        assert load_json_metadata(str(tmp_path), "movie") == records

    def test_overwrite_replaces_previous_content(self, tmp_path):
        save_json_metadata(str(tmp_path), "movie", [{"media_id": "tmdb_1", "filename": "A.mp4"}])
        save_json_metadata(str(tmp_path), "movie", [{"media_id": "tmdb_2", "filename": "B.mp4"}])

        assert load_json_metadata(str(tmp_path), "movie") == [{"media_id": "tmdb_2", "filename": "B.mp4"}]

    def test_no_temp_file_left_behind(self, tmp_path):
        save_json_metadata(str(tmp_path), "movie", [{"media_id": "tmdb_1", "filename": "A.mp4"}])
        path = _json_path(str(tmp_path), "movie")
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []

    def test_cleans_up_temp_file_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        path = _json_path(str(tmp_path), "movie")
        save_json_metadata(str(tmp_path), "movie", [{"media_id": "tmdb_1", "filename": "A.mp4"}])
        original = path.read_text(encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(annotate.os, "replace", _boom)
        with pytest.raises(OSError):
            save_json_metadata(str(tmp_path), "movie", [{"media_id": "tmdb_2", "filename": "B.mp4"}])

        assert path.read_text(encoding="utf-8") == original
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []

    def test_serialization_format_unchanged(self, tmp_path):
        save_json_metadata(str(tmp_path), "movie", [{"media_id": "tmdb_1", "title": "Café"}])
        path = _json_path(str(tmp_path), "movie")
        text = path.read_text(encoding="utf-8")

        expected = json.dumps(
            {"version": "1", "media": [{"media_id": "tmdb_1", "title": "Café"}]},
            indent=2, ensure_ascii=False,
        ) + "\n"
        assert text == expected


class TestUpsertJsonRecord:
    def test_upsert_round_trip_via_atomic_write(self, tmp_path):
        upsert_json_record(str(tmp_path), {"media_id": "tmdb_1", "title": "Film"}, "movie")
        upsert_json_record(str(tmp_path), {"media_id": "tmdb_1", "title": "Film Updated"}, "movie")

        records = load_json_metadata(str(tmp_path), "movie")
        assert records == [{"media_id": "tmdb_1", "title": "Film Updated"}]

        path = _json_path(str(tmp_path), "movie")
        leftovers = [p for p in path.parent.iterdir() if p != path]
        assert leftovers == []
