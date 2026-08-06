"""Tests for the atomic-write path used by annotation JSON writers.

`data/annotate.py`, `services/frame_match.py`, and
`visualizers/shot_visualizer.py` all rewrite the same canonical
``<stem>.annotations.json`` file in place. They used to call
`Path.write_text()`/`open(path, "w")` directly, so a crash mid-write could
leave the file truncated or corrupt. All of them now go through
`data.annotate.atomic_write_text()`, which writes to a same-directory temp
file and swaps it in with `os.replace()`.
"""

import json

import pytest

from data import annotate
from services.frame_match import _save_annotation_entries


@pytest.fixture
def ann_file(tmp_path):
    """A project-shaped annotations directory with a starter JSON file."""
    d = tmp_path / "data" / "annotations" / "shots" / "movie"
    d.mkdir(parents=True)
    path = d / "Film.annotations.json"
    return path


class TestAtomicWriteText:
    def test_creates_file_with_exact_content(self, tmp_path):
        dest = tmp_path / "sub" / "Film.annotations.json"
        text = json.dumps([{"shot": {"shot_id": "a@f000000-f000030"}}], indent=2, ensure_ascii=False)
        annotate.atomic_write_text(dest, text)
        assert dest.read_text(encoding="utf-8") == text

    def test_no_temp_file_left_behind_after_success(self, tmp_path):
        dest = tmp_path / "Film.annotations.json"
        annotate.atomic_write_text(dest, "[]")
        leftovers = [p for p in tmp_path.iterdir() if p != dest]
        assert leftovers == []

    def test_cleans_up_temp_file_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        dest = tmp_path / "Film.annotations.json"
        dest.write_text("[1]", encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(annotate.os, "replace", _boom)
        with pytest.raises(OSError):
            annotate.atomic_write_text(dest, "[2]")

        assert dest.read_text(encoding="utf-8") == "[1]"
        leftovers = [p for p in tmp_path.iterdir() if p != dest]
        assert leftovers == []


class TestReindexAnnotationsForMerge:
    def test_removes_matching_entries_and_preserves_others(self, tmp_path, ann_file):
        entries = [
            {"shot": {"shot_id": "keep@f000000-f000030"}},
            {"shot": {"shot_id": "drop@f000030-f000060"}},
        ]
        ann_file.write_text(json.dumps(entries, indent=2), encoding="utf-8")

        annotate.reindex_annotations_for_merge(
            str(tmp_path), "Film.mp4", "movie", {"drop@f000030-f000060"},
        )

        remaining = json.loads(ann_file.read_text(encoding="utf-8"))
        assert [e["shot"]["shot_id"] for e in remaining] == ["keep@f000000-f000030"]

    def test_no_temp_file_left_behind(self, tmp_path, ann_file):
        ann_file.write_text(json.dumps([{"shot": {"shot_id": "a"}}]), encoding="utf-8")
        annotate.reindex_annotations_for_merge(str(tmp_path), "Film.mp4", "movie", {"a"})
        leftovers = [p for p in ann_file.parent.iterdir() if p != ann_file]
        assert leftovers == []

    def test_noop_when_file_absent(self, tmp_path):
        # Should not raise even though no annotation file exists yet.
        annotate.reindex_annotations_for_merge(str(tmp_path), "Film.mp4", "movie", {"a"})


class TestReindexAnnotationsForSplit:
    def test_removes_matching_entries_and_preserves_others(self, tmp_path, ann_file):
        entries = [
            {"shot": {"shot_id": "keep@f000000-f000030"}},
            {"shot": {"shot_id": "split@f000030-f000060"}},
        ]
        ann_file.write_text(json.dumps(entries, indent=2), encoding="utf-8")

        annotate.reindex_annotations_for_split(
            str(tmp_path), "Film.mp4", "movie", {"split@f000030-f000060"},
        )

        remaining = json.loads(ann_file.read_text(encoding="utf-8"))
        assert [e["shot"]["shot_id"] for e in remaining] == ["keep@f000000-f000030"]


class TestSaveAnnotationEntries:
    def test_round_trips_content(self, ann_file):
        entries = [{"shot": {"shot_id": "a@f000000-f000030", "best_frame": {"frame": 5}}}]
        _save_annotation_entries(ann_file, entries)
        assert json.loads(ann_file.read_text(encoding="utf-8")) == entries

    def test_no_temp_file_left_behind(self, ann_file):
        _save_annotation_entries(ann_file, [])
        leftovers = [p for p in ann_file.parent.iterdir() if p != ann_file]
        assert leftovers == []

    def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "data" / "annotations" / "shots" / "trailer" / "New.annotations.json"
        _save_annotation_entries(nested, [])
        assert nested.exists()
