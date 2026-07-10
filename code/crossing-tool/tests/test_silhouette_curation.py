"""Tests for services/silhouette_curation.py.

Covers:
- label_bucket: letter → uppercase bucket, non-letter → '#'
- set_human_best: writes/removes human_best field in JSON
- clear_human_best_for_label: clears all except excluded path, in-memory + disk
- mark_best: sets target, clears others, in-memory + disk round-trip
- unmark_best: removes marker, returns correct bool
- find_best_in_records: returns correct index or -1
- Persistence round-trip: scan_catalog picks up human_best after write
- Loading: find_best_in_records works on scan_catalog output
"""

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from services.silhouette_curation import (
    HUMAN_BEST_FIELD,
    clear_human_best_for_label,
    find_best_in_records,
    label_bucket,
    mark_best,
    set_human_best,
    unmark_best,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_catalog_entry(
    label_dir: Path,
    index: int,
    extra: dict | None = None,
) -> Path:
    """Write a minimal object PNG + JSON pair and return the JSON path."""
    label_dir.mkdir(parents=True, exist_ok=True)
    stem = f"object_{index:04d}"

    png_path = label_dir / f"{stem}.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 128)).save(png_path)

    meta = {
        "schema_version": "1",
        "media_type": "movie",
        "filename": "Test Film (1934) {tmdb-000001}.mp4",
        "filename_stem": "Test Film (1934) {tmdb-000001}",
        "media_id": "tmdb_1",
        "shot_id": f"tmdb_1@f00{index:04d}-f00{index+1:04d}",
        "frame": index * 100,
        "label": label_dir.name,
        "field": "objects",
        "confidence": 0.8 - index * 0.1,
        "bbox": [10, 20, 30, 40],
        "mask_area": 1200,
        "frame_size": [1920, 1080],
        "png": f"{stem}.png",
    }
    if extra:
        meta.update(extra)

    json_path = label_dir / f"{stem}.json"
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return json_path


def _rec(json_path: Path) -> dict:
    """Load a record dict as scan_catalog would return it."""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    data["path"] = json_path
    return data


# ---------------------------------------------------------------------------
# label_bucket
# ---------------------------------------------------------------------------

class TestLabelBucket(unittest.TestCase):
    def test_letter_uppercased(self):
        self.assertEqual(label_bucket("horse"), "H")

    def test_already_upper(self):
        self.assertEqual(label_bucket("Horse"), "H")

    def test_number_gives_hash(self):
        self.assertEqual(label_bucket("123"), "#")

    def test_special_char_gives_hash(self):
        self.assertEqual(label_bucket("_underscore"), "#")

    def test_empty_gives_hash(self):
        self.assertEqual(label_bucket(""), "#")

    def test_z(self):
        self.assertEqual(label_bucket("zebra"), "Z")

    def test_multi_word(self):
        self.assertEqual(label_bucket("fire tools"), "F")


# ---------------------------------------------------------------------------
# set_human_best
# ---------------------------------------------------------------------------

class TestSetHumanBest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.label_dir = self.tmp / "horse"
        self.json_path = _make_catalog_entry(self.label_dir, 1)

    def tearDown(self):
        self._tmp.cleanup()

    def test_set_true_writes_field(self):
        set_human_best(self.json_path, human_best=True)
        data = json.loads(self.json_path.read_text())
        self.assertTrue(data.get(HUMAN_BEST_FIELD))

    def test_set_false_removes_field(self):
        set_human_best(self.json_path, human_best=True)
        set_human_best(self.json_path, human_best=False)
        data = json.loads(self.json_path.read_text())
        self.assertNotIn(HUMAN_BEST_FIELD, data)

    def test_set_false_when_absent_is_noop(self):
        set_human_best(self.json_path, human_best=False)
        data = json.loads(self.json_path.read_text())
        self.assertNotIn(HUMAN_BEST_FIELD, data)

    def test_preserves_other_fields(self):
        set_human_best(self.json_path, human_best=True)
        data = json.loads(self.json_path.read_text())
        self.assertEqual(data["label"], "horse")
        self.assertIn("confidence", data)

    def test_raises_when_file_missing(self):
        with self.assertRaises(FileNotFoundError):
            set_human_best(self.tmp / "nonexistent.json", human_best=True)


# ---------------------------------------------------------------------------
# clear_human_best_for_label
# ---------------------------------------------------------------------------

class TestClearHumanBestForLabel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.label_dir = self.tmp / "horse"

    def tearDown(self):
        self._tmp.cleanup()

    def _make_recs(self, n: int, best_idx: int | None = None) -> list[dict]:
        recs = []
        for i in range(1, n + 1):
            jp = _make_catalog_entry(self.label_dir, i)
            if best_idx is not None and i - 1 == best_idx:
                set_human_best(jp, human_best=True)
            recs.append(_rec(jp))
        return recs

    def test_clears_all_best_flags(self):
        recs = self._make_recs(3, best_idx=1)
        self.assertTrue(recs[1].get(HUMAN_BEST_FIELD))
        clear_human_best_for_label(recs)
        for rec in recs:
            self.assertFalse(rec.get(HUMAN_BEST_FIELD))
            data = json.loads(Path(rec["path"]).read_text())
            self.assertNotIn(HUMAN_BEST_FIELD, data)

    def test_except_path_is_untouched(self):
        recs = self._make_recs(3, best_idx=1)
        except_path = recs[1]["path"]
        clear_human_best_for_label(recs, except_path=except_path)
        self.assertTrue(recs[1].get(HUMAN_BEST_FIELD))
        data = json.loads(Path(except_path).read_text())
        self.assertTrue(data.get(HUMAN_BEST_FIELD))

    def test_no_best_is_noop(self):
        recs = self._make_recs(3)
        clear_human_best_for_label(recs)  # should not raise
        for rec in recs:
            self.assertFalse(rec.get(HUMAN_BEST_FIELD))


# ---------------------------------------------------------------------------
# mark_best
# ---------------------------------------------------------------------------

class TestMarkBest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.label_dir = self.tmp / "horse"
        self.paths = [_make_catalog_entry(self.label_dir, i) for i in range(1, 4)]
        self.recs = [_rec(p) for p in self.paths]

    def tearDown(self):
        self._tmp.cleanup()

    def test_sets_target_as_best(self):
        result = mark_best(self.recs[0], self.recs)
        self.assertTrue(result)
        self.assertTrue(self.recs[0].get(HUMAN_BEST_FIELD))

    def test_does_not_clear_previous_best(self):
        """mark_best is non-exclusive: marking a new object leaves others marked."""
        mark_best(self.recs[2], self.recs)
        mark_best(self.recs[0], self.recs)
        self.assertTrue(self.recs[0].get(HUMAN_BEST_FIELD))
        # recs[2] stays marked — not cleared
        self.assertTrue(self.recs[2].get(HUMAN_BEST_FIELD))

    def test_persisted_to_disk(self):
        mark_best(self.recs[1], self.recs)
        data = json.loads(self.paths[1].read_text())
        self.assertTrue(data.get(HUMAN_BEST_FIELD))
        # Others are not touched (still absent unless marked)
        for i in (0, 2):
            d = json.loads(self.paths[i].read_text())
            self.assertNotIn(HUMAN_BEST_FIELD, d)

    def test_returns_false_for_missing_file(self):
        bad_rec = {"path": self.tmp / "ghost.json"}
        result = mark_best(bad_rec, self.recs)
        self.assertFalse(result)

    def test_returns_false_for_no_path(self):
        result = mark_best({}, self.recs)
        self.assertFalse(result)

    def test_multiple_best_allowed(self):
        """Multiple objects in a label group can be marked simultaneously."""
        mark_best(self.recs[0], self.recs)
        mark_best(self.recs[1], self.recs)
        best_count = sum(1 for r in self.recs if r.get(HUMAN_BEST_FIELD))
        self.assertEqual(best_count, 2)


# ---------------------------------------------------------------------------
# unmark_best
# ---------------------------------------------------------------------------

class TestUnmarkBest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.label_dir = self.tmp / "horse"
        self.json_path = _make_catalog_entry(self.label_dir, 1)
        self.rec = _rec(self.json_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_removes_marker(self):
        self.rec[HUMAN_BEST_FIELD] = True
        set_human_best(self.json_path, human_best=True)
        result = unmark_best(self.rec)
        self.assertTrue(result)
        self.assertFalse(self.rec.get(HUMAN_BEST_FIELD))
        data = json.loads(self.json_path.read_text())
        self.assertNotIn(HUMAN_BEST_FIELD, data)

    def test_returns_false_when_not_marked(self):
        result = unmark_best(self.rec)
        self.assertFalse(result)

    def test_in_memory_cleared(self):
        self.rec[HUMAN_BEST_FIELD] = True
        set_human_best(self.json_path, human_best=True)
        unmark_best(self.rec)
        self.assertNotIn(HUMAN_BEST_FIELD, self.rec)


# ---------------------------------------------------------------------------
# find_best_in_records
# ---------------------------------------------------------------------------

class TestFindBestInRecords(unittest.TestCase):
    def test_returns_index_of_best(self):
        recs = [{}, {HUMAN_BEST_FIELD: True}, {}]
        self.assertEqual(find_best_in_records(recs), 1)

    def test_returns_minus_one_when_absent(self):
        recs = [{}, {}, {}]
        self.assertEqual(find_best_in_records(recs), -1)

    def test_returns_first_match(self):
        recs = [{HUMAN_BEST_FIELD: True}, {HUMAN_BEST_FIELD: True}]
        self.assertEqual(find_best_in_records(recs), 0)

    def test_empty_list(self):
        self.assertEqual(find_best_in_records([]), -1)


# ---------------------------------------------------------------------------
# Persistence round-trip via scan_catalog
# ---------------------------------------------------------------------------

class TestPersistenceRoundTrip(unittest.TestCase):
    """Verify that human_best survives a full scan_catalog re-load."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Build a minimal catalog structure
        self.catalog_dir = self.tmp / "data" / "silhouettes" / "catalog" / "movie"
        self.label_dir = self.catalog_dir / "Test Film (1934) {tmdb-000001}" / "horse"
        self.json_paths = [_make_catalog_entry(self.label_dir, i) for i in range(1, 4)]

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_catalog_includes_human_best(self):
        # Mark object_0002 as best
        set_human_best(self.json_paths[1], human_best=True)

        from services.silhouette_catalog import scan_catalog
        records = scan_catalog(str(self.tmp), media_type="movie")

        best_recs = [r for r in records if r.get(HUMAN_BEST_FIELD)]
        self.assertEqual(len(best_recs), 1)
        self.assertEqual(Path(best_recs[0]["path"]).name, "object_0002.json")

    def test_find_best_after_scan(self):
        set_human_best(self.json_paths[2], human_best=True)

        from services.silhouette_catalog import scan_catalog
        records = scan_catalog(str(self.tmp), media_type="movie")
        idx = find_best_in_records(records)
        self.assertNotEqual(idx, -1)
        self.assertEqual(Path(records[idx]["path"]).name, "object_0003.json")

    def test_unmark_then_rescan_has_no_best(self):
        set_human_best(self.json_paths[0], human_best=True)
        set_human_best(self.json_paths[0], human_best=False)

        from services.silhouette_catalog import scan_catalog
        records = scan_catalog(str(self.tmp), media_type="movie")
        self.assertEqual(find_best_in_records(records), -1)
