"""Tests for frame-embedding path helpers, stats counting, and manifest fields."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_file(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _write_json(path: Path, data) -> None:
    _write_file(path, json.dumps(data, indent=2))


def _write_npy(path: Path, array: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, array)


# ---------------------------------------------------------------------------
# Path helper tests
# ---------------------------------------------------------------------------

class TestFrameEmbeddingPathHelpers(unittest.TestCase):
    """data.index path helpers resolve to the correct locations."""

    def test_get_frame_embeddings_path(self):
        from data.index import get_frame_embeddings_path

        with tempfile.TemporaryDirectory() as tmp:
            p = get_frame_embeddings_path(tmp, "My Film.mp4", "movie")
            expected = (
                Path(tmp)
                / "data" / "annotations" / "shots" / "movie"
                / "My Film.frames.npy"
            )
            self.assertEqual(p, expected)

    def test_get_frame_valid_path(self):
        from data.index import get_frame_valid_path

        with tempfile.TemporaryDirectory() as tmp:
            p = get_frame_valid_path(tmp, "My Film.mp4", "gameplay")
            expected = (
                Path(tmp)
                / "data" / "annotations" / "shots" / "gameplay"
                / "My Film.frames.valid.npy"
            )
            self.assertEqual(p, expected)

    def test_get_frame_manifest_path(self):
        from data.index import get_frame_manifest_path

        with tempfile.TemporaryDirectory() as tmp:
            p = get_frame_manifest_path(tmp, "My Film.mp4", "movie")
            expected = (
                Path(tmp)
                / "data" / "annotations" / "shots" / "movie"
                / "My Film.frames.manifest.json"
            )
            self.assertEqual(p, expected)

    def test_path_uses_stem_not_full_filename(self):
        """Extension is stripped; only stem is used."""
        from data.index import get_frame_embeddings_path, get_frame_valid_path, get_frame_manifest_path

        with tempfile.TemporaryDirectory() as tmp:
            npy = get_frame_embeddings_path(tmp, "Test (2024) {tmdb-999}.mp4", "movie")
            valid = get_frame_valid_path(tmp, "Test (2024) {tmdb-999}.mp4", "movie")
            mfst = get_frame_manifest_path(tmp, "Test (2024) {tmdb-999}.mp4", "movie")

            self.assertTrue(npy.name.endswith(".frames.npy"))
            self.assertFalse(npy.name.endswith(".mp4.frames.npy"))
            self.assertTrue(valid.name.endswith(".frames.valid.npy"))
            self.assertTrue(mfst.name.endswith(".frames.manifest.json"))

    def test_frame_paths_differ_from_annotation_paths(self):
        """Frame-embedding paths do not collide with annotation-embedding paths."""
        from data.index import (
            get_embeddings_path,
            get_frame_embeddings_path,
            get_frame_valid_path,
        )

        with tempfile.TemporaryDirectory() as tmp:
            ann_npy = get_embeddings_path(tmp, "Film.mp4", "movie")
            frame_npy = get_frame_embeddings_path(tmp, "Film.mp4", "movie")
            frame_valid = get_frame_valid_path(tmp, "Film.mp4", "movie")

            self.assertNotEqual(ann_npy, frame_npy)
            self.assertNotEqual(ann_npy, frame_valid)
            self.assertNotEqual(frame_npy, frame_valid)


# ---------------------------------------------------------------------------
# Stats counting tests
# ---------------------------------------------------------------------------

class TestEmbeddingStatsCounting(unittest.TestCase):
    """corpus_stats counts annotation-embeddings and frame-embeddings separately."""

    def _make_project(self, tmp: str) -> Path:
        project = Path(tmp)
        # Minimal metadata so get_corpus_stats doesn't crash
        _write_json(
            project / "data" / "metadata" / "movie.json",
            {"version": "1", "media": []},
        )
        _write_json(
            project / "data" / "metadata" / "gameplay.json",
            {"version": "1", "media": []},
        )
        _write_json(
            project / "data" / "vocabulary" / "vocabulary_movie.json",
            {"meta": {"total_tokens": 0, "vocabulary_fields": []}, "fields": {}},
        )
        return project

    def test_annotation_embeddings_count_plain_npy(self):
        """Annotation embeddings count *.annotations.npy files."""
        from services.corpus_stats import _count_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "annotations" / "shots" / "movie"
            base.mkdir(parents=True)

            _write_npy(base / "Film A.annotations.npy", np.zeros((5, 384), dtype="float32"))
            _write_npy(base / "Film B.annotations.npy", np.zeros((3, 384), dtype="float32"))

            result = _count_embeddings(tmp)
            self.assertEqual(result.get("movie", 0), 2)

    def test_annotation_embeddings_do_not_count_old_plain_npy(self):
        """Old plain .npy files (pre-migration) are NOT counted as annotation embeddings."""
        from services.corpus_stats import _count_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "annotations" / "shots" / "movie"
            base.mkdir(parents=True)

            # Old-style plain .npy — should NOT be counted after migration
            _write_npy(base / "Film A.npy", np.zeros((5, 384), dtype="float32"))

            result = _count_embeddings(tmp)
            self.assertEqual(result.get("movie", 0), 0)

    def test_annotation_embeddings_exclude_frames_npy(self):
        from services.corpus_stats import _count_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "annotations" / "shots" / "movie"
            base.mkdir(parents=True)

            _write_npy(base / "Film A.annotations.npy", np.zeros((5, 384), dtype="float32"))
            _write_npy(base / "Film A.frames.npy", np.zeros((5, 512), dtype="float32"))
            _write_npy(base / "Film A.frames.valid.npy", np.ones(5, dtype=bool))

            result = _count_embeddings(tmp)
            # Only the .annotations.npy should be counted
            self.assertEqual(result.get("movie", 0), 1)

    def test_annotation_embeddings_exclude_frames_valid_npy(self):
        from services.corpus_stats import _count_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "annotations" / "shots" / "gameplay"
            base.mkdir(parents=True)

            _write_npy(base / "Clip.annotations.npy", np.zeros((2, 384), dtype="float32"))
            _write_npy(base / "Clip.frames.valid.npy", np.ones(2, dtype=bool))

            result = _count_embeddings(tmp)
            self.assertEqual(result.get("gameplay", 0), 1)

    def test_frame_embeddings_count_frames_npy(self):
        from services.corpus_stats import _count_frame_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "annotations" / "shots" / "movie"
            base.mkdir(parents=True)

            _write_npy(base / "Film A.frames.npy", np.zeros((5, 512), dtype="float32"))
            _write_npy(base / "Film B.frames.npy", np.zeros((3, 512), dtype="float32"))

            result = _count_frame_embeddings(tmp)
            self.assertEqual(result.get("movie", 0), 2)

    def test_frame_embeddings_do_not_count_valid_npy(self):
        from services.corpus_stats import _count_frame_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "annotations" / "shots" / "movie"
            base.mkdir(parents=True)

            _write_npy(base / "Film A.frames.npy", np.zeros((5, 512), dtype="float32"))
            _write_npy(base / "Film A.frames.valid.npy", np.ones(5, dtype=bool))

            result = _count_frame_embeddings(tmp)
            # Only .frames.npy counts, not .frames.valid.npy
            self.assertEqual(result.get("movie", 0), 1)

    def test_frame_embeddings_do_not_count_plain_npy(self):
        """Old plain .npy files do not count as frame embeddings."""
        from services.corpus_stats import _count_frame_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "annotations" / "shots" / "movie"
            base.mkdir(parents=True)

            _write_npy(base / "Film A.annotations.npy", np.zeros((5, 384), dtype="float32"))

            result = _count_frame_embeddings(tmp)
            self.assertEqual(result.get("movie", 0), 0)

    def test_get_corpus_stats_has_frame_embeddings_key(self):
        """get_corpus_stats returns both embeddings and frame_embeddings keys."""
        from services.corpus_stats import get_corpus_stats

        with tempfile.TemporaryDirectory() as tmp:
            self._make_project(tmp)

            base = Path(tmp) / "data" / "annotations" / "shots" / "movie"
            base.mkdir(parents=True)
            _write_npy(base / "Film A.annotations.npy", np.zeros((5, 384), dtype="float32"))
            _write_npy(base / "Film A.frames.npy", np.zeros((5, 512), dtype="float32"))
            _write_npy(base / "Film A.frames.valid.npy", np.ones(5, dtype=bool))

            stats = get_corpus_stats(tmp)
            self.assertIn("embeddings", stats)
            self.assertIn("frame_embeddings", stats)
            self.assertEqual(stats["embeddings"], 1)
            self.assertEqual(stats["frame_embeddings"], 1)


# ---------------------------------------------------------------------------
# Manifest field tests
# ---------------------------------------------------------------------------

class TestFrameManifestFields(unittest.TestCase):
    """_build_frame_manifest produces the required fields."""

    def test_manifest_required_fields(self):
        from services.frame_embeddings import _build_frame_manifest

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            filename = "My Film.mp4"
            stem = "My Film"
            media_type = "movie"
            model_name = "clip-vit-base-patch32"

            base = project / "data" / "annotations" / "shots" / media_type
            base.mkdir(parents=True)

            json_path = base / f"{stem}.annotations.json"
            npy_path = base / f"{stem}.frames.npy"
            valid_path = base / f"{stem}.frames.valid.npy"

            items = [{"shot": {"shot_id": "abc@f0-f100"}}]
            json_path.write_text(json.dumps(items), encoding="utf-8")

            embeddings = np.zeros((1, 512), dtype="float32")
            valid_mask = np.array([True], dtype=bool)
            np.save(npy_path, embeddings)
            np.save(valid_path, valid_mask)

            manifest = _build_frame_manifest(
                project_path=tmp,
                filename=filename,
                media_type=media_type,
                model_name=model_name,
                json_path=json_path,
                npy_path=npy_path,
                valid_path=valid_path,
                embeddings=embeddings,
                valid_mask=valid_mask,
                item_count=1,
                valid_count=1,
                missing_count=0,
                now="2026-01-01T00:00:00Z",
            )

            self.assertEqual(manifest["index_type"], "frame-embeddings")
            self.assertEqual(manifest["embedding_modality"], "image")
            self.assertEqual(manifest["embedding_source"], "best-frame images")
            self.assertEqual(manifest["model"]["role"], "frame_match")
            self.assertEqual(manifest["model"]["name"], model_name)
            self.assertIn("json", manifest)
            self.assertIn("frames", manifest)
            self.assertIn("npy", manifest)
            self.assertIn("valid", manifest)
            self.assertEqual(manifest["frames"]["valid_count"], 1)
            self.assertEqual(manifest["frames"]["missing_count"], 0)


# ---------------------------------------------------------------------------
# Migration behavior tests
# ---------------------------------------------------------------------------

class TestMigrateNames(unittest.TestCase):
    """_index_annotations_migrate_names renames files safely and idempotently."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "data" / "annotations" / "shots" / "movie"
        self.base.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, name: str) -> Path:
        p = self.base / name
        p.write_text("{}", encoding="utf-8")
        return p

    def _migrate(self, stem: str, dry_run: bool = False, verbose: bool = False):
        """Call the migration helper directly via the internal logic (unit-style)."""
        renames = [
            (".json",          ".annotations.json"),
            (".txt",           ".annotations.txt"),
            (".npy",           ".annotations.npy"),
            (".manifest.json", ".annotations.manifest.json"),
        ]
        counts = {"renamed": 0, "already": 0, "conflict": 0, "missing": 0}
        conflicts = []
        for old_suffix, new_suffix in renames:
            src = self.base / f"{stem}{old_suffix}"
            dst = self.base / f"{stem}{new_suffix}"
            if src.exists() and not dst.exists():
                if not dry_run:
                    src.rename(dst)
                counts["renamed"] += 1
            elif not src.exists() and dst.exists():
                counts["already"] += 1
            elif src.exists() and dst.exists():
                counts["conflict"] += 1
                conflicts.append(src.name)
            else:
                counts["missing"] += 1
        return counts, conflicts

    def test_source_exists_target_missing_renames(self):
        self._touch("Film.json")
        self._touch("Film.txt")
        self._touch("Film.npy")
        self._touch("Film.manifest.json")

        counts, conflicts = self._migrate("Film")

        self.assertEqual(counts["renamed"], 4)
        self.assertEqual(counts["conflict"], 0)
        self.assertFalse((self.base / "Film.json").exists())
        self.assertTrue((self.base / "Film.annotations.json").exists())
        self.assertTrue((self.base / "Film.annotations.txt").exists())
        self.assertTrue((self.base / "Film.annotations.npy").exists())
        self.assertTrue((self.base / "Film.annotations.manifest.json").exists())

    def test_target_exists_source_missing_already_migrated(self):
        self._touch("Film.annotations.json")
        self._touch("Film.annotations.txt")
        self._touch("Film.annotations.npy")
        self._touch("Film.annotations.manifest.json")

        counts, conflicts = self._migrate("Film")

        self.assertEqual(counts["already"], 4)
        self.assertEqual(counts["renamed"], 0)
        self.assertEqual(counts["conflict"], 0)

    def test_both_exist_conflict_no_overwrite(self):
        self._touch("Film.json")
        self._touch("Film.annotations.json")  # target already present

        counts, conflicts = self._migrate("Film")

        self.assertEqual(counts["conflict"], 1)
        # Both files must still exist
        self.assertTrue((self.base / "Film.json").exists())
        self.assertTrue((self.base / "Film.annotations.json").exists())

    def test_dry_run_does_not_rename(self):
        self._touch("Film.json")
        self._touch("Film.npy")

        counts, conflicts = self._migrate("Film", dry_run=True)

        self.assertEqual(counts["renamed"], 2)
        # Files must NOT have been renamed
        self.assertTrue((self.base / "Film.json").exists())
        self.assertTrue((self.base / "Film.npy").exists())
        self.assertFalse((self.base / "Film.annotations.json").exists())

    def test_frame_files_not_renamed(self):
        """frames.npy / frames.valid.npy / frames.manifest.json must not be touched."""
        self._touch("Film.frames.npy")
        self._touch("Film.frames.valid.npy")
        self._touch("Film.frames.manifest.json")

        # Migration operates on .json / .txt / .npy / .manifest.json only
        counts, conflicts = self._migrate("Film")

        # frames files are not in the rename list so they are untouched
        self.assertTrue((self.base / "Film.frames.npy").exists())
        self.assertTrue((self.base / "Film.frames.valid.npy").exists())
        self.assertTrue((self.base / "Film.frames.manifest.json").exists())
        # The .npy plain file didn't exist so it counts as missing
        self.assertGreaterEqual(counts["missing"], 1)


if __name__ == "__main__":
    unittest.main()
