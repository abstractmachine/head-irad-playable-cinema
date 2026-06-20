"""Tests for services/motif_audit.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.motif_audit import audit_motifs_for_file, audit_motifs_for_all, _is_present


# ---------------------------------------------------------------------------
# _is_present
# ---------------------------------------------------------------------------

class TestIsPresent(unittest.TestCase):
    def test_normal_string(self):
        self.assertTrue(_is_present("riding"))

    def test_whitespace_only(self):
        self.assertFalse(_is_present("   "))

    def test_empty_string(self):
        self.assertFalse(_is_present(""))

    def test_none(self):
        self.assertFalse(_is_present(None))

    def test_dict(self):
        self.assertFalse(_is_present({"value": "riding"}))

    def test_integer(self):
        self.assertFalse(_is_present(42))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_annotation(path: Path, entries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _make_shot(shot_id: str, motif=None, annotation_motif=None) -> dict:
    """Build a canonical annotation entry."""
    ann = {}
    if annotation_motif is not None:
        ann["motif"] = annotation_motif
    shot: dict = {"shot_id": shot_id, "annotation": ann}
    if motif is not None:
        shot["motif"] = motif
    return {"shot": shot}


# ---------------------------------------------------------------------------
# audit_motifs_for_file
# ---------------------------------------------------------------------------

class TestAuditMotifsForFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _ann_path(self, media_type: str, stem: str) -> Path:
        return self.project / "data" / "annotations" / "shots" / media_type / f"{stem}.annotations.json"

    def test_all_present(self):
        _write_annotation(self._ann_path("movie", "film-a"), [
            _make_shot("s1", motif="riding"),
            _make_shot("s2", motif="duel"),
        ])
        r = audit_motifs_for_file(str(self.project), "film-a.mp4", "movie")
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["present"], 2)
        self.assertEqual(r["missing"], 0)
        self.assertEqual(r["missing_shot_ids"], [])

    def test_some_missing(self):
        _write_annotation(self._ann_path("movie", "film-b"), [
            _make_shot("s1", motif="snow"),
            _make_shot("s2"),            # no motif key
            _make_shot("s3", motif=""),  # empty string → missing
        ])
        r = audit_motifs_for_file(str(self.project), "film-b.mp4", "movie")
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["present"], 1)
        self.assertEqual(r["missing"], 2)
        self.assertIn("s2", r["missing_shot_ids"])
        self.assertIn("s3", r["missing_shot_ids"])

    def test_zero_motifs(self):
        _write_annotation(self._ann_path("movie", "film-c"), [
            _make_shot("s1"),
            _make_shot("s2"),
        ])
        r = audit_motifs_for_file(str(self.project), "film-c.mp4", "movie")
        self.assertEqual(r["present"], 0)
        self.assertEqual(r["missing"], 2)

    def test_annotation_motif_ignored(self):
        """shot.annotation.motif must NOT be counted — only shot.motif."""
        _write_annotation(self._ann_path("movie", "film-d"), [
            _make_shot("s1", annotation_motif="riding"),  # inside annotation, not canonical
        ])
        r = audit_motifs_for_file(str(self.project), "film-d.mp4", "movie")
        self.assertEqual(r["present"], 0)
        self.assertEqual(r["missing"], 1)

    def test_dict_motif_ignored(self):
        """A dict shot.motif is not canonical."""
        entry = {"shot": {"shot_id": "s1", "annotation": {}, "motif": {"value": "riding"}}}
        _write_annotation(self._ann_path("movie", "film-e"), [entry])
        r = audit_motifs_for_file(str(self.project), "film-e.mp4", "movie")
        self.assertEqual(r["present"], 0)

    def test_null_motif_missing(self):
        entry = {"shot": {"shot_id": "s1", "annotation": {}, "motif": None}}
        _write_annotation(self._ann_path("movie", "film-f"), [entry])
        r = audit_motifs_for_file(str(self.project), "film-f.mp4", "movie")
        self.assertEqual(r["present"], 0)
        self.assertEqual(r["missing"], 1)

    def test_whitespace_motif_missing(self):
        entry = {"shot": {"shot_id": "s1", "annotation": {}, "motif": "   "}}
        _write_annotation(self._ann_path("movie", "film-g"), [entry])
        r = audit_motifs_for_file(str(self.project), "film-g.mp4", "movie")
        self.assertEqual(r["present"], 0)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            audit_motifs_for_file(str(self.project), "nonexistent.mp4", "movie")

    def test_sidecar_not_read(self):
        """A data/motifs/ sidecar must be ignored."""
        sidecar = self.project / "data" / "motifs" / "movie" / "film-h.json"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"shots": [{"shot_id": "s1", "value": "legacy"}] * 50}))
        # No annotation JSON exists → FileNotFoundError, not a sidecar count
        with self.assertRaises(FileNotFoundError):
            audit_motifs_for_file(str(self.project), "film-h.mp4", "movie")


# ---------------------------------------------------------------------------
# audit_motifs_for_all
# ---------------------------------------------------------------------------

class TestAuditMotifsForAll(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _ann_path(self, media_type: str, stem: str) -> Path:
        return self.project / "data" / "annotations" / "shots" / media_type / f"{stem}.annotations.json"

    def test_corpus_counts(self):
        _write_annotation(self._ann_path("movie", "film-a"), [
            _make_shot("s1", motif="riding"),
            _make_shot("s2", motif="duel"),
        ])
        _write_annotation(self._ann_path("movie", "film-b"), [
            _make_shot("s3"),            # missing
            _make_shot("s4", motif="snow"),
        ])
        r = audit_motifs_for_all(str(self.project), "movie")
        self.assertEqual(r["files_checked"], 2)
        self.assertEqual(r["shots_checked"], 4)
        self.assertEqual(r["motifs_present"], 3)
        self.assertEqual(r["motifs_missing"], 1)
        self.assertAlmostEqual(r["coverage"], 0.75)

    def test_sorted_by_missing_desc(self):
        _write_annotation(self._ann_path("movie", "alpha"), [
            _make_shot("s1"),  # 1 missing
        ])
        _write_annotation(self._ann_path("movie", "beta"), [
            _make_shot("s2"),  # 1 missing
            _make_shot("s3"),  # 1 missing
        ])
        r = audit_motifs_for_all(str(self.project), "movie")
        self.assertEqual(r["files"][0]["filename"], "beta")  # more missing first

    def test_missing_dir_returns_empty(self):
        r = audit_motifs_for_all(str(self.project), "movie")
        self.assertEqual(r["files_checked"], 0)
        self.assertEqual(r["shots_checked"], 0)
        self.assertEqual(r["coverage"], 1.0)

    def test_manifest_json_skipped(self):
        """*.manifest.json files must not be counted as annotation files."""
        # Create a file with the new canonical manifest name
        ann_dir = self.project / "data" / "annotations" / "shots" / "movie"
        ann_dir.mkdir(parents=True, exist_ok=True)
        manifest = ann_dir / "film-a.annotations.manifest.json"
        manifest.write_text(json.dumps({"index_type": "annotation-embeddings"}))
        _write_annotation(self._ann_path("movie", "film-a"), [
            _make_shot("s1", motif="riding"),
        ])
        r = audit_motifs_for_all(str(self.project), "movie")
        self.assertEqual(r["files_checked"], 1)

    def test_sidecar_not_read(self):
        """data/motifs/ sidecars must be entirely ignored."""
        sidecar = self.project / "data" / "motifs" / "movie" / "film-x.json"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"shots": [{"shot_id": "s1", "value": "legacy"}] * 100}))
        # No annotation JSON files at all
        r = audit_motifs_for_all(str(self.project), "movie")
        self.assertEqual(r["motifs_present"], 0)
        self.assertEqual(r["files_checked"], 0)
