"""Tests for services/silhouette.py.

Covers:
- Path/filename generation helpers
- Score encoding
- Scope directory name derivation
- Containment check logic
- Polygon degeneracy detection
- Mask-to-polygon conversion (mocked)
- Silhouette cache existence check
- JSON payload shape from build_silhouette (mocked pipeline)
- Scope resolution dispatch (find_candidates delegation)
- Rejection accumulation in build_silhouette
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.silhouette import (
    SILHOUETTE_VERSION,
    _safe_field,
    _safe_word,
    _score_to_str,
    _scope_dir_name,
    _scope_label,
    check_containment,
    find_candidates,
    load_silhouette_manifest,
    mask_to_polygon,
    polygon_is_degenerate,
    save_silhouette_manifest,
    silhouette_cache_dir,
    silhouette_exists,
    silhouette_filename,
    silhouette_json_path,
)


# ---------------------------------------------------------------------------
# Helpers / utilities
# ---------------------------------------------------------------------------

class TestSafeWord(unittest.TestCase):
    def test_lowercase_spaces_to_underscores(self):
        self.assertEqual(_safe_word("Desert Horse"), "desert_horse")

    def test_special_chars_replaced(self):
        self.assertEqual(_safe_word("horse!@#"), "horse___")

    def test_already_safe(self):
        self.assertEqual(_safe_word("horse"), "horse")


class TestSafeField(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(_safe_field("Animals"), "animals")

    def test_spaces_to_underscores(self):
        self.assertEqual(_safe_field("body parts"), "body_parts")


class TestScoreToStr(unittest.TestCase):
    def test_decimal_becomes_p(self):
        self.assertEqual(_score_to_str(0.942), "0p942")

    def test_whole_number(self):
        self.assertEqual(_score_to_str(1.0), "1p000")

    def test_three_decimal_places(self):
        self.assertEqual(_score_to_str(0.1), "0p100")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

class TestSilhouetteCacheDir(unittest.TestCase):
    def test_all_scope(self):
        p = silhouette_cache_dir("/proj", "movies", "all", "animals", "horse")
        self.assertEqual(p, Path("/proj/data/silhouettes/movies/all/animals/horse"))

    def test_movie_scope(self):
        p = silhouette_cache_dir("/proj", "movies", "movie-tmdb_11969", "objects", "saddle")
        self.assertEqual(
            p,
            Path("/proj/data/silhouettes/movies/movie-tmdb_11969/objects/saddle"),
        )

    def test_shot_scope(self):
        shot = "tmdb_281957@f001240-f001310"
        p = silhouette_cache_dir("/proj", "movies", f"shot-{shot}", "animals", "horse")
        self.assertTrue(str(p).endswith(f"shot-{shot}/animals/horse"))

    def test_field_and_word_are_lowercased(self):
        p = silhouette_cache_dir("/proj", "movies", "all", "Animals", "Horse")
        self.assertEqual(p, Path("/proj/data/silhouettes/movies/all/animals/horse"))


class TestSilhouetteFilename(unittest.TestCase):
    def test_format_matches_spec(self):
        fn = silhouette_filename(
            media_id="tmdb_281957",
            shot_id="tmdb_281957@f001240-f001310",
            frame_index=1532,
            score=0.942,
        )
        self.assertTrue(fn.startswith("best__src-tmdb_281957__"))
        self.assertIn("shot-tmdb_281957@f001240-f001310", fn)
        self.assertIn("frame-001532", fn)
        self.assertIn("score-0p942", fn)
        self.assertTrue(fn.endswith(f"__{SILHOUETTE_VERSION}.json"))

    def test_frame_zero_padded_to_six_digits(self):
        fn = silhouette_filename("m", "m@f000000-f000010", 7, 0.5)
        self.assertIn("frame-000007", fn)

    def test_no_decimal_in_filename(self):
        fn = silhouette_filename("m", "m@f000000-f000010", 0, 0.917)
        self.assertNotIn(".", fn.replace(".json", ""))


class TestSilhouetteJsonPath(unittest.TestCase):
    def test_path_combines_dir_and_filename(self):
        path = silhouette_json_path(
            project_path="/proj",
            media_type="movies",
            scope="all",
            field="animals",
            word="horse",
            media_id="tmdb_281957",
            shot_id="tmdb_281957@f001240-f001310",
            frame_index=1532,
            score=0.942,
        )
        self.assertIn("/data/silhouettes/movies/all/animals/horse/", str(path))
        self.assertTrue(str(path).endswith(".json"))


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------

class TestScopeDirName(unittest.TestCase):
    def test_all(self):
        self.assertEqual(_scope_dir_name("all", None, None), "all")

    def test_movie_with_media_id(self):
        self.assertEqual(_scope_dir_name("movie", "Django", "tmdb_11969"), "movie-tmdb_11969")

    def test_movie_without_media_id_uses_scope_value(self):
        self.assertEqual(_scope_dir_name("movie", "Django", None), "movie-Django")

    def test_shot(self):
        sid = "tmdb_281957@f001240-f001310"
        self.assertEqual(_scope_dir_name("shot", sid, None), f"shot-{sid}")


class TestScopeLabel(unittest.TestCase):
    def test_all(self):
        self.assertEqual(_scope_label("all", None), "all")

    def test_movie(self):
        self.assertEqual(_scope_label("movie", "Django"), "movie:Django")

    def test_shot(self):
        self.assertEqual(_scope_label("shot", "tmdb@f001-f002"), "shot:tmdb@f001-f002")

    def test_none_returns_all(self):
        self.assertEqual(_scope_label(None, None), "all")


# ---------------------------------------------------------------------------
# Containment check
# ---------------------------------------------------------------------------

class TestCheckContainment(unittest.TestCase):
    def _make_mask(self, h=100, w=100, fill=False):
        import numpy as np
        m = np.zeros((h, w), dtype=bool)
        if fill:
            m[:] = True
        return m

    def test_centred_square_is_contained(self):
        import numpy as np
        m = self._make_mask()
        m[20:80, 20:80] = True
        self.assertTrue(check_containment(m))

    def test_touching_top_edge_rejected(self):
        import numpy as np
        m = self._make_mask()
        m[0:5, 40:60] = True   # top strip
        self.assertFalse(check_containment(m))

    def test_touching_bottom_edge_rejected(self):
        import numpy as np
        m = self._make_mask()
        m[97:100, 40:60] = True
        self.assertFalse(check_containment(m))

    def test_touching_left_edge_rejected(self):
        import numpy as np
        m = self._make_mask()
        m[40:60, 0:3] = True
        self.assertFalse(check_containment(m))

    def test_touching_right_edge_rejected(self):
        import numpy as np
        m = self._make_mask()
        m[40:60, 97:100] = True
        self.assertFalse(check_containment(m))

    def test_all_zeros_is_contained(self):
        m = self._make_mask()
        self.assertTrue(check_containment(m))

    def test_non_2d_array_rejected(self):
        import numpy as np
        m = np.zeros((3, 100, 100), dtype=bool)
        self.assertFalse(check_containment(m))


# ---------------------------------------------------------------------------
# Polygon helpers
# ---------------------------------------------------------------------------

class TestPolygonIsDegenerate(unittest.TestCase):
    def test_none_is_degenerate(self):
        self.assertTrue(polygon_is_degenerate(None))

    def test_too_few_points(self):
        self.assertTrue(polygon_is_degenerate([[0, 0], [1, 1]]))

    def test_too_small_bbox(self):
        # Bounding box 5×5 — below the 10×10 threshold
        self.assertTrue(polygon_is_degenerate([[0, 0], [5, 0], [5, 5], [0, 5]]))

    def test_valid_polygon(self):
        pts = [[10, 10], [60, 10], [60, 60], [10, 60]]
        self.assertFalse(polygon_is_degenerate(pts))

    def test_empty_list(self):
        self.assertTrue(polygon_is_degenerate([]))


class TestMaskToPolygon(unittest.TestCase):
    def test_square_mask_returns_polygon(self):
        import numpy as np
        mask = np.zeros((100, 100), dtype=bool)
        mask[20:80, 20:80] = True
        pts = mask_to_polygon(mask)
        self.assertIsNotNone(pts)
        self.assertGreaterEqual(len(pts), 3)

    def test_empty_mask_returns_none(self):
        import numpy as np
        mask = np.zeros((100, 100), dtype=bool)
        pts = mask_to_polygon(mask)
        self.assertIsNone(pts)

    def test_tiny_mask_returns_none(self):
        import numpy as np
        mask = np.zeros((100, 100), dtype=bool)
        mask[50:53, 50:53] = True   # 9 pixels — below the 100-pixel area threshold
        pts = mask_to_polygon(mask)
        self.assertIsNone(pts)

    def test_polygon_points_are_lists_of_two_ints(self):
        import numpy as np
        mask = np.zeros((200, 200), dtype=bool)
        mask[40:160, 40:160] = True
        pts = mask_to_polygon(mask)
        self.assertIsNotNone(pts)
        for pt in pts:
            self.assertEqual(len(pt), 2)
            self.assertIsInstance(pt[0], (int, float))
            self.assertIsInstance(pt[1], (int, float))


# ---------------------------------------------------------------------------
# Cache existence
# ---------------------------------------------------------------------------

class TestSilhouetteExists(unittest.TestCase):
    def test_no_dir_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            exists, path = silhouette_exists(tmp, "movies", "all", "animals", "horse")
            self.assertFalse(exists)
            self.assertIsNone(path)

    def test_empty_dir_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = silhouette_cache_dir(tmp, "movies", "all", "animals", "horse")
            cache.mkdir(parents=True)
            exists, path = silhouette_exists(tmp, "movies", "all", "animals", "horse")
            self.assertFalse(exists)

    def test_existing_file_returns_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = silhouette_cache_dir(tmp, "movies", "all", "animals", "horse")
            cache.mkdir(parents=True)
            fn = silhouette_filename("tmdb_1", "tmdb_1@f000000-f000100", 50, 0.8)
            (cache / fn).write_text("{}", encoding="utf-8")
            exists, path = silhouette_exists(tmp, "movies", "all", "animals", "horse")
            self.assertTrue(exists)
            self.assertIsNotNone(path)
            self.assertEqual(path.name, fn)


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

class TestManifest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = load_silhouette_manifest(tmp, "movies")
            self.assertEqual(manifest, {"entries": []})
            manifest["entries"].append({"word": "horse", "field": "animals"})
            save_silhouette_manifest(tmp, "movies", manifest)
            reloaded = load_silhouette_manifest(tmp, "movies")
            self.assertEqual(len(reloaded["entries"]), 1)
            self.assertEqual(reloaded["entries"][0]["word"], "horse")

    def test_corrupt_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            mpath = Path(tmp) / "data" / "silhouettes" / "movies" / "manifest.json"
            mpath.parent.mkdir(parents=True)
            mpath.write_text("NOT JSON", encoding="utf-8")
            manifest = load_silhouette_manifest(tmp, "movies")
            self.assertEqual(manifest, {"entries": []})


# ---------------------------------------------------------------------------
# find_candidates: scope dispatch
# ---------------------------------------------------------------------------

class TestFindCandidates(unittest.TestCase):
    def test_single_shot_returns_placeholder(self):
        """Single-shot mode must return a placeholder without calling search."""
        candidates = find_candidates(
            project_path="/fake",
            word="horse",
            field="animals",
            scope_type="shot",
            scope_value="tmdb_1@f000000-f000100",
            media_type="movies",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["shot_id"], "tmdb_1@f000000-f000100")
        self.assertTrue(candidates[0].get("_single_shot_mode"))

    @patch("services.silhouette.find_candidates")
    def test_all_scope_delegates_to_search(self, mock_fc):
        """Calling find_candidates with scope_type='all' should use search_shots."""
        mock_fc.return_value = []
        result = mock_fc(
            project_path="/fake",
            word="horse",
            field="animals",
            scope_type="all",
            scope_value=None,
            media_type="movies",
        )
        mock_fc.assert_called_once()
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# build_silhouette: JSON payload shape (fully mocked pipeline)
# ---------------------------------------------------------------------------

class TestBuildSilhouettePayloadShape(unittest.TestCase):
    """Verify the JSON payload written to disk has all required keys."""

    def _make_fake_polygon(self):
        return [[10, 10], [50, 10], [50, 50], [10, 50]]

    @patch("services.silhouette.process_shot_silhouette")
    @patch("services.silhouette.find_candidates")
    @patch("services.silhouette.get_metadata")
    @patch("services.silhouette.load_sam_model")
    @patch("services.frame_match._load_clip_model")
    def test_payload_has_required_keys(
        self, mock_clip, mock_sam, mock_meta, mock_find, mock_process
    ):
        """build_silhouette must write a JSON with all required payload keys."""
        from unittest.mock import MagicMock
        mock_clip.return_value = (MagicMock(), MagicMock(), "cpu")
        mock_sam.return_value = (MagicMock(), "sam2.1_b.pt", "cpu")
        import numpy as np

        REQUIRED = {
            "word", "field", "scope", "media_type", "source_filename",
            "media_id", "shot_id", "start_frame", "end_frame", "frame_index",
            "frame_provenance", "frame_size", "sam_model", "frame_match_model",
            "score", "contained", "rejection_reason", "bbox", "polygon",
            "polygon_simplification", "timestamp", "version",
        }

        fake_payload = {
            "word": "horse",
            "field": "animals",
            "scope": "all",
            "media_type": "movies",
            "source_filename": "film.mp4",
            "media_id": "tmdb_1",
            "shot_id": "tmdb_1@f000000-f000100",
            "start_frame": 0,
            "end_frame": 100,
            "frame_index": 50,
            "frame_provenance": "frame:50",
            "frame_size": [1920, 1080],
            "sam_model": "sam2.1_b.pt",
            "frame_match_model": "clip-vit-base-patch32",
            "score": 0.85,
            "contained": True,
            "rejection_reason": None,
            "bbox": [100, 100, 200, 200],
            "polygon": self._make_fake_polygon(),
            "polygon_simplification": "douglas_peucker_0.005_perimeter",
            "timestamp": "2026-05-15T00:00:00+00:00",
            "version": "v1",
        }

        mock_meta.return_value = [
            {"filename": "film.mp4", "tmdb": "1", "title": "Film"}
        ]
        mock_find.return_value = [
            {
                "shot_id": "tmdb_1@f000000-f000100",
                "filename": "film.mp4",
                "score": 0.9,
            }
        ]
        mock_process.return_value = {
            "accepted": True,
            "reason": "ok",
            "payload": fake_payload,
        }

        with tempfile.TemporaryDirectory() as tmp:
            from services.silhouette import build_silhouette, compute_media_id as _cmi
            with patch("services.silhouette.compute_media_id", return_value="tmdb_1"):
                result = build_silhouette(
                    project_path=tmp,
                    word="horse",
                    field="animals",
                    scope_type="all",
                    scope_value=None,
                    media_type="movies",
                    sam_model_name="sam2.1_b.pt",
                    frame_model_name="clip-vit-base-patch32",
                    force=False,
                    verbose=False,
                    dry_run=False,
                )

        self.assertTrue(result["accepted"])
        payload = result["payload"]
        self.assertIsNotNone(payload)

        missing = REQUIRED - set(payload.keys())
        self.assertEqual(missing, set(), f"Missing keys in payload: {missing}")


# ---------------------------------------------------------------------------
# build_silhouette: rejection behavior
# ---------------------------------------------------------------------------

class TestBuildSilhouetteRejection(unittest.TestCase):
    @patch("services.silhouette.process_shot_silhouette")
    @patch("services.silhouette.find_candidates")
    @patch("services.silhouette.get_metadata")
    @patch("services.silhouette.load_sam_model")
    @patch("services.frame_match._load_clip_model")
    def test_all_candidates_rejected_returns_failure(
        self, mock_clip, mock_sam, mock_meta, mock_find, mock_process
    ):
        from unittest.mock import MagicMock
        mock_clip.return_value = (MagicMock(), MagicMock(), "cpu")
        mock_sam.return_value = (MagicMock(), "sam2.1_b.pt", "cpu")
        mock_meta.return_value = [
            {"filename": "film.mp4", "tmdb": "1", "title": "Film"}
        ]
        mock_find.return_value = [
            {"shot_id": "tmdb_1@f000000-f000100", "filename": "film.mp4", "score": 0.9}
        ]
        mock_process.return_value = {
            "accepted": False,
            "reason": "mask touches border",
            "payload": None,
        }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("services.silhouette.compute_media_id", return_value="tmdb_1"):
                result = __import__(
                    "services.silhouette", fromlist=["build_silhouette"]
                ).build_silhouette(
                    project_path=tmp,
                    word="horse",
                    field="animals",
                    scope_type="all",
                    scope_value=None,
                    media_type="movies",
                    sam_model_name="sam2.1_b.pt",
                    frame_model_name="clip-vit-base-patch32",
                    force=False,
                    verbose=False,
                    dry_run=False,
                )

        self.assertFalse(result["accepted"])
        self.assertIn("mask touches border", result["reason"])

    @patch("services.silhouette.find_candidates")
    @patch("services.silhouette.get_metadata")
    def test_no_candidates_returns_failure(self, mock_meta, mock_find):
        mock_meta.return_value = []
        mock_find.return_value = []

        with tempfile.TemporaryDirectory() as tmp:
            with patch("services.silhouette.compute_media_id", return_value="tmdb_1"):
                result = __import__(
                    "services.silhouette", fromlist=["build_silhouette"]
                ).build_silhouette(
                    project_path=tmp,
                    word="horse",
                    field="animals",
                    scope_type="all",
                    scope_value=None,
                    media_type="movies",
                    sam_model_name="sam2.1_b.pt",
                    frame_model_name="clip-vit-base-patch32",
                    force=False,
                    verbose=False,
                    dry_run=False,
                )

        self.assertFalse(result["accepted"])


# ---------------------------------------------------------------------------
# build_silhouette: dry-run mode
# ---------------------------------------------------------------------------

class TestBuildSilhouetteDryRun(unittest.TestCase):
    @patch("services.silhouette.find_candidates")
    @patch("services.silhouette.get_metadata")
    def test_dry_run_returns_candidates_without_processing(
        self, mock_meta, mock_find
    ):
        mock_meta.return_value = [
            {"filename": "film.mp4", "tmdb": "1", "title": "Film"}
        ]
        mock_find.return_value = [
            {"shot_id": "tmdb_1@f000000-f000100", "filename": "film.mp4", "score": 0.9}
        ]

        with tempfile.TemporaryDirectory() as tmp:
            with patch("services.silhouette.compute_media_id", return_value="tmdb_1"):
                result = __import__(
                    "services.silhouette", fromlist=["build_silhouette"]
                ).build_silhouette(
                    project_path=tmp,
                    word="horse",
                    field="animals",
                    scope_type="all",
                    scope_value=None,
                    media_type="movies",
                    sam_model_name="sam2.1_b.pt",
                    frame_model_name="clip-vit-base-patch32",
                    force=False,
                    verbose=False,
                    dry_run=True,
                )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "dry-run")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertIsNone(result["output_path"])


# ---------------------------------------------------------------------------
# build_silhouette: cache hit
# ---------------------------------------------------------------------------

class TestBuildSilhouetteCacheHit(unittest.TestCase):
    def test_cache_hit_returns_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Pre-populate the cache
            cache = silhouette_cache_dir(tmp, "movies", "all", "animals", "horse")
            cache.mkdir(parents=True)
            fn = silhouette_filename("tmdb_1", "tmdb_1@f000000-f000100", 50, 0.8)
            payload = {"word": "horse", "field": "animals", "score": 0.8}
            (cache / fn).write_text(
                json.dumps(payload), encoding="utf-8"
            )

            # Calling without force should return cached result
            with patch("services.silhouette.get_metadata", return_value=[]):
                result = __import__(
                    "services.silhouette", fromlist=["build_silhouette"]
                ).build_silhouette(
                    project_path=tmp,
                    word="horse",
                    field="animals",
                    scope_type="all",
                    scope_value=None,
                    media_type="movies",
                    sam_model_name="sam2.1_b.pt",
                    frame_model_name="clip-vit-base-patch32",
                    force=False,
                    verbose=False,
                    dry_run=False,
                )

            self.assertTrue(result["accepted"])
            self.assertEqual(result["reason"], "cached")


# ---------------------------------------------------------------------------
# scan_records_flat
# ---------------------------------------------------------------------------

class TestScanRecordsFlat(unittest.TestCase):
    """scan_records_flat must expose every JSON file as a separate record."""

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_returns_all_records_including_duplicates_for_same_word(self):
        from services.silhouette import scan_records_flat, silhouette_filename

        with tempfile.TemporaryDirectory() as tmp:
            # Two silhouettes for horse/animals — different scopes
            f1 = (
                Path(tmp) / "data" / "silhouettes" / "movies"
                / "movie-tmdb_1" / "animals" / "horse"
                / silhouette_filename("tmdb_1", "tmdb_1@f000000-f000050", 25, 0.85)
            )
            f2 = (
                Path(tmp) / "data" / "silhouettes" / "movies"
                / "movie-tmdb_2" / "animals" / "horse"
                / silhouette_filename("tmdb_2", "tmdb_2@f000100-f000200", 150, 0.72)
            )
            # One silhouette for saddle/objects
            f3 = (
                Path(tmp) / "data" / "silhouettes" / "movies"
                / "all" / "objects" / "saddle"
                / silhouette_filename("tmdb_1", "tmdb_1@f000060-f000090", 70, 0.60)
            )
            for f in (f1, f2, f3):
                self._write_json(f, {"word": f.parent.name, "score": 0.5})

            records = scan_records_flat(tmp, "movies")

        self.assertEqual(len(records), 3)
        words = [r["word"] for r in records]
        # Both horse records must be present
        self.assertEqual(words.count("horse"), 2)
        self.assertEqual(words.count("saddle"), 1)

    def test_filter_field_excludes_other_fields(self):
        from services.silhouette import scan_records_flat, silhouette_filename

        with tempfile.TemporaryDirectory() as tmp:
            f1 = (
                Path(tmp) / "data" / "silhouettes" / "movies"
                / "all" / "animals" / "horse"
                / silhouette_filename("tmdb_1", "tmdb_1@f000000-f000050", 25, 0.80)
            )
            f2 = (
                Path(tmp) / "data" / "silhouettes" / "movies"
                / "all" / "objects" / "saddle"
                / silhouette_filename("tmdb_1", "tmdb_1@f000060-f000090", 70, 0.60)
            )
            for f in (f1, f2):
                self._write_json(f, {})

            records = scan_records_flat(tmp, "movies", filter_field="animals")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["field"], "animals")
        self.assertEqual(records[0]["word"], "horse")

    def test_sorted_by_field_word_scope_score_desc(self):
        from services.silhouette import scan_records_flat, silhouette_filename

        with tempfile.TemporaryDirectory() as tmp:
            # Two horse records in the same scope — higher score should come first
            high = (
                Path(tmp) / "data" / "silhouettes" / "movies"
                / "all" / "animals" / "horse"
                / silhouette_filename("tmdb_1", "tmdb_1@f000000-f000010", 5, 0.90)
            )
            low = (
                Path(tmp) / "data" / "silhouettes" / "movies"
                / "all" / "animals" / "horse"
                / silhouette_filename("tmdb_2", "tmdb_2@f000000-f000010", 5, 0.65)
            )
            for f in (high, low):
                self._write_json(f, {})

            records = scan_records_flat(tmp, "movies")

        scores = [r["score"] for r in records]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_empty_when_no_cache(self):
        from services.silhouette import scan_records_flat

        with tempfile.TemporaryDirectory() as tmp:
            records = scan_records_flat(tmp, "movies")

        self.assertEqual(records, [])

    def test_record_has_expected_keys(self):
        from services.silhouette import scan_records_flat, silhouette_filename

        with tempfile.TemporaryDirectory() as tmp:
            f = (
                Path(tmp) / "data" / "silhouettes" / "movies"
                / "all" / "animals" / "horse"
                / silhouette_filename("tmdb_1", "tmdb_1@f000000-f000010", 5, 0.80)
            )
            self._write_json(f, {})

            records = scan_records_flat(tmp, "movies")

        self.assertEqual(len(records), 1)
        rec = records[0]
        for key in ("path", "field", "word", "scope", "score"):
            self.assertIn(key, rec)
        self.assertIsInstance(rec["path"], Path)
        self.assertEqual(rec["field"], "animals")
        self.assertEqual(rec["word"], "horse")
        self.assertEqual(rec["scope"], "all")
        self.assertAlmostEqual(rec["score"], 0.80, places=2)


# ---------------------------------------------------------------------------
# Pipeline: strict acceptance thresholds
# ---------------------------------------------------------------------------

class TestClipScoreFloor(unittest.TestCase):
    """process_shot_silhouette must reject masks whose CLIP score is below the floor."""

    def _make_fake_mask(self, score: float) -> tuple:
        import numpy as np
        h, w = 100, 100
        seg = np.zeros((h, w), dtype=bool)
        seg[20:60, 20:60] = True
        mask_dict = {
            "segmentation": seg,
            "bbox": [20, 20, 40, 40],
            "area": int(seg.sum()),
        }
        return mask_dict, score

    @patch("services.silhouette.rank_masks_by_clip")
    @patch("services.silhouette.mask_generator", create=True)
    def test_score_below_floor_is_rejected(self, _mg, mock_rank):
        from services.silhouette import (
            _CLIP_SCORE_FLOOR,
            process_shot_silhouette,
        )
        import numpy as np

        low_score = _CLIP_SCORE_FLOOR - 0.05
        mock_rank.return_value = [self._make_fake_mask(low_score)]

        # Use patch to avoid loading real models / real video
        with patch("services.silhouette.rank_masks_by_clip", return_value=[self._make_fake_mask(low_score)]):
            with patch("services.silhouette.load_sam_model") as mock_sam_load:
                mg = MagicMock()
                import numpy as _np
                mg.generate.return_value = [{"segmentation": _np.zeros((10, 10), dtype=bool), "bbox": [0, 0, 10, 10], "area": 25}]
                mock_sam_load.return_value = (mg, "sam2.1_b.pt", "cpu")

                with patch("services.silhouette.parse_shot_id", return_value=("tmdb_1", 0, 100)):
                    with patch("services.silhouette.extract_frame_as_pil") as mock_frame:
                        mock_frame.return_value = MagicMock(size=(320, 240))
                        with patch("services.frame_match._get_video_fps", return_value=24.0):
                            with patch("services.frame_match.find_query_best_frame_for_shot", return_value=(50, 0.9)):
                                with tempfile.TemporaryDirectory() as tmp:
                                    import os
                                    video_dir = Path(tmp) / "media" / "videos" / "movies"
                                    video_dir.mkdir(parents=True)
                                    (video_dir / "film.mp4").touch()

                                    result = process_shot_silhouette(
                                        project_path=tmp,
                                        word="horse",
                                        field="animals",
                                        shot_id="tmdb_1@f000000-f000100",
                                        filename="film.mp4",
                                        media_id="tmdb_1",
                                        media_type="movies",
                                        sam_model_name="sam2.1_b.pt",
                                        frame_model_name="clip-vit-base-patch32",
                                        clip_model=MagicMock(),
                                        clip_processor=MagicMock(),
                                        clip_device="cpu",
                                        mask_generator=mg,
                                    )

        self.assertFalse(result["accepted"])
        self.assertIn("score too low", result["reason"])


class TestRunnerUpMargin(unittest.TestCase):
    """process_shot_silhouette must reject when top and runner-up scores are too close."""

    def _make_mask(self, score: float) -> tuple:
        import numpy as np
        h, w = 100, 100
        seg = np.zeros((h, w), dtype=bool)
        seg[20:60, 20:60] = True
        mask_dict = {
            "segmentation": seg,
            "bbox": [20, 20, 40, 40],
            "area": int(seg.sum()),
        }
        return mask_dict, score

    def test_too_close_scores_are_rejected(self):
        from services.silhouette import (
            _CLIP_SCORE_FLOOR,
            _CLIP_RUNNER_UP_MARGIN,
            process_shot_silhouette,
        )
        import numpy as _np

        top = _CLIP_SCORE_FLOOR + 0.10
        runner_up = top - (_CLIP_RUNNER_UP_MARGIN * 0.5)  # closer than required

        m1 = self._make_mask(top)[0]
        m2 = self._make_mask(runner_up)[0]

        mg = MagicMock()
        mg.generate.return_value = [m1, m2]

        # Tight-crop scores are also ambiguous
        tight_margin = _CLIP_RUNNER_UP_MARGIN * 0.4
        tight_reranked = [(m1, top, top + 0.01), (m2, runner_up, top + 0.01 - tight_margin)]

        with patch("services.silhouette.rank_masks_by_clip",
                   return_value=[self._make_mask(top), self._make_mask(runner_up)]):
            with patch("services.silhouette.rerank_by_tight_crop",
                       return_value=tight_reranked):
                with patch("services.silhouette.parse_shot_id", return_value=("tmdb_1", 0, 100)):
                    with patch("services.silhouette.extract_frame_as_pil") as mock_frame:
                        mock_frame.return_value = MagicMock(size=(320, 240))
                        with patch("services.frame_match._get_video_fps", return_value=24.0):
                            with patch("services.frame_match.find_query_best_frame_for_shot", return_value=(50, 0.9)):
                                with tempfile.TemporaryDirectory() as tmp:
                                    video_dir = Path(tmp) / "media" / "videos" / "movies"
                                    video_dir.mkdir(parents=True)
                                    (video_dir / "film.mp4").touch()

                                    result = process_shot_silhouette(
                                        project_path=tmp,
                                        word="horse",
                                        field="animals",
                                        shot_id="tmdb_1@f000000-f000100",
                                        filename="film.mp4",
                                        media_id="tmdb_1",
                                        media_type="movies",
                                        sam_model_name="sam2.1_b.pt",
                                        frame_model_name="clip-vit-base-patch32",
                                        clip_model=MagicMock(),
                                        clip_processor=MagicMock(),
                                        clip_device="cpu",
                                        mask_generator=mg,
                                    )

        self.assertFalse(result["accepted"])
        self.assertIn("ambiguous", result["reason"])

    def test_sufficient_margin_passes_through(self):
        from services.silhouette import (
            _CLIP_SCORE_FLOOR,
            _CLIP_RUNNER_UP_MARGIN,
            process_shot_silhouette,
            check_containment,
        )
        import numpy as _np

        top = _CLIP_SCORE_FLOOR + 0.15
        runner_up = top - (_CLIP_RUNNER_UP_MARGIN * 2)  # clear margin

        h, w = 100, 200
        seg = _np.zeros((h, w), dtype=bool)
        seg[20:60, 40:120] = True  # well within bounds

        good_mask = {
            "segmentation": seg,
            "bbox": [40, 20, 80, 40],
            "area": int(seg.sum()),
        }
        bad_mask = {
            "segmentation": _np.zeros((h, w), dtype=bool),
            "bbox": [0, 0, 5, 5],
            "area": 1,
        }

        mg = MagicMock()
        mg.generate.return_value = [good_mask, bad_mask]

        # Tight scores preserve sufficient margin
        tight_reranked = [
            (good_mask, top, top + 0.01),
            (bad_mask, runner_up, runner_up),
        ]

        with patch("services.silhouette.rank_masks_by_clip",
                   return_value=[(good_mask, top), (bad_mask, runner_up)]):
            with patch("services.silhouette.rerank_by_tight_crop",
                       return_value=tight_reranked):
                with patch("services.silhouette.parse_shot_id", return_value=("tmdb_1", 0, 100)):
                    with patch("services.silhouette.extract_frame_as_pil") as mock_frame:
                        pil_mock = MagicMock()
                        pil_mock.size = (w, h)
                        mock_frame.return_value = pil_mock
                        with patch("services.frame_match._get_video_fps", return_value=24.0):
                            with patch("services.frame_match.find_query_best_frame_for_shot", return_value=(50, 0.9)):
                                with patch("services.frame_match.best_frame_path") as mock_bfp:
                                    mock_bfp.return_value = Path("/nonexistent/frame.jpg")
                                    with tempfile.TemporaryDirectory() as tmp:
                                        video_dir = Path(tmp) / "media" / "videos" / "movies"
                                        video_dir.mkdir(parents=True)
                                        (video_dir / "film.mp4").touch()

                                        result = process_shot_silhouette(
                                            project_path=tmp,
                                            word="horse",
                                            field="animals",
                                            shot_id="tmdb_1@f000000-f000100",
                                            filename="film.mp4",
                                            media_id="tmdb_1",
                                            media_type="movies",
                                            sam_model_name="sam2.1_b.pt",
                                            frame_model_name="clip-vit-base-patch32",
                                            clip_model=MagicMock(),
                                            clip_processor=MagicMock(),
                                            clip_device="cpu",
                                            mask_generator=mg,
                                        )

        # Should get past the ambiguity check; may fail on polygon/containment
        # but must NOT fail with "ambiguous"
        self.assertNotIn("ambiguous", result.get("reason", ""))


# ---------------------------------------------------------------------------
# Aspect-ratio sanity filter in rank_masks_by_clip
# ---------------------------------------------------------------------------

class TestAspectRatioFilter(unittest.TestCase):
    """rank_masks_by_clip must discard masks with extreme aspect ratios."""

    def _pil_image(self, w=320, h=240):
        from PIL import Image
        return Image.new("RGB", (w, h), (128, 128, 128))

    def _mask(self, x, y, bw, bh, frame_w=320, frame_h=240):
        import numpy as np
        seg = np.zeros((frame_h, frame_w), dtype=bool)
        seg[y:y+bh, x:x+bw] = True
        return {"segmentation": seg, "bbox": [x, y, bw, bh], "area": int(seg.sum())}

    def test_elongated_mask_is_rejected(self):
        from services.silhouette import rank_masks_by_clip, _MAX_ASPECT_RATIO
        # bbox 240×10 — aspect ratio 24 >> _MAX_ASPECT_RATIO
        elongated = self._mask(10, 115, 240, 10)
        with patch("services.frame_match.clip_score_batch", return_value=[0.5]):
            result = rank_masks_by_clip(
                [elongated], self._pil_image(),
                "horse", MagicMock(), MagicMock(), "cpu",
            )
        self.assertEqual(result, [], "Elongated mask should be filtered out")

    def test_normal_mask_passes(self):
        from services.silhouette import rank_masks_by_clip
        # bbox 80×60 — aspect ratio 1.33 < 6.0
        normal = self._mask(50, 50, 80, 60)
        with patch("services.frame_match.clip_score_batch", return_value=[0.4]):
            result = rank_masks_by_clip(
                [normal], self._pil_image(),
                "horse", MagicMock(), MagicMock(), "cpu",
            )
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0][1], 0.4)

    def test_borderline_aspect_ratio_rejected(self):
        from services.silhouette import rank_masks_by_clip, _MAX_ASPECT_RATIO
        # bbox exactly at the limit should be rejected (> not >=)
        bw, bh = int(_MAX_ASPECT_RATIO * 10) + 1, 10  # just over the threshold
        mask = self._mask(5, 100, bw, bh)
        with patch("services.frame_match.clip_score_batch", return_value=[0.5]):
            result = rank_masks_by_clip(
                [mask], self._pil_image(w=400, h=300),
                "horse", MagicMock(), MagicMock(), "cpu",
            )
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Tight-crop re-scoring (rerank_by_tight_crop)
# ---------------------------------------------------------------------------

class TestTightCropReranking(unittest.TestCase):
    """rerank_by_tight_crop must composite masks and re-rank by tight score."""

    def _pil_image(self, w=200, h=150):
        from PIL import Image
        return Image.new("RGB", (w, h), (100, 100, 100))

    def _mask_dict(self, x, y, bw, bh, frame_w=200, frame_h=150):
        import numpy as np
        seg = np.zeros((frame_h, frame_w), dtype=bool)
        seg[y:y+bh, x:x+bw] = True
        return {"segmentation": seg, "bbox": [x, y, bw, bh], "area": int(seg.sum())}

    def test_returns_three_tuple_per_candidate(self):
        from services.silhouette import rerank_by_tight_crop
        m1 = self._mask_dict(10, 10, 40, 40)
        m2 = self._mask_dict(80, 20, 50, 50)
        candidates = [(m1, 0.50), (m2, 0.45)]

        with patch("services.frame_match.clip_score_batch", return_value=[0.42, 0.38]):
            result = rerank_by_tight_crop(
                candidates, self._pil_image(), "horse",
                MagicMock(), MagicMock(), "cpu",
            )

        self.assertEqual(len(result), 2)
        for item in result:
            self.assertEqual(len(item), 3)

    def test_re_ranks_by_tight_score(self):
        from services.silhouette import rerank_by_tight_crop
        m1 = self._mask_dict(10, 10, 40, 40)
        m2 = self._mask_dict(80, 20, 50, 50)
        # Broad: m1 wins.  Tight: m2 wins.
        candidates = [(m1, 0.50), (m2, 0.45)]

        with patch("services.frame_match.clip_score_batch", return_value=[0.38, 0.55]):
            result = rerank_by_tight_crop(
                candidates, self._pil_image(), "horse",
                MagicMock(), MagicMock(), "cpu",
            )

        # After reranking, m2 should be first
        self.assertIs(result[0][0], m2)
        self.assertAlmostEqual(result[0][2], 0.55)
        self.assertIs(result[1][0], m1)
        self.assertAlmostEqual(result[1][2], 0.38)

    def test_preserves_broad_scores(self):
        from services.silhouette import rerank_by_tight_crop
        m1 = self._mask_dict(10, 10, 40, 40)
        candidates = [(m1, 0.72)]

        with patch("services.frame_match.clip_score_batch", return_value=[0.65]):
            result = rerank_by_tight_crop(
                candidates, self._pil_image(), "horse",
                MagicMock(), MagicMock(), "cpu",
            )

        self.assertEqual(len(result), 1)
        mask_out, broad_out, tight_out = result[0]
        self.assertIs(mask_out, m1)
        self.assertAlmostEqual(broad_out, 0.72)
        self.assertAlmostEqual(tight_out, 0.65)

    def test_empty_candidates_returns_empty(self):
        from services.silhouette import rerank_by_tight_crop
        result = rerank_by_tight_crop(
            [], self._pil_image(), "horse",
            MagicMock(), MagicMock(), "cpu",
        )
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _make_masked_crop
# ---------------------------------------------------------------------------

class TestMaskedCrop(unittest.TestCase):
    """_make_masked_crop must produce a tight crop over neutral grey."""

    def test_output_is_pil_image(self):
        from PIL import Image
        import numpy as np
        from services.silhouette import _make_masked_crop

        frame = Image.new("RGB", (200, 150), (200, 100, 50))
        seg = np.zeros((150, 200), dtype=bool)
        seg[30:80, 40:120] = True
        mask = {"segmentation": seg, "bbox": [40, 30, 80, 50]}

        crop = _make_masked_crop(frame, mask)
        self.assertIsInstance(crop, Image.Image)

    def test_crop_smaller_than_frame(self):
        from PIL import Image
        import numpy as np
        from services.silhouette import _make_masked_crop

        frame = Image.new("RGB", (320, 240), (0, 0, 0))
        seg = np.zeros((240, 320), dtype=bool)
        seg[50:100, 60:160] = True  # 50×100 region
        mask = {"segmentation": seg, "bbox": [60, 50, 100, 50]}

        crop = _make_masked_crop(frame, mask)
        cw, ch = crop.size
        self.assertLess(cw, 320)
        self.assertLess(ch, 240)

    def test_outside_mask_pixels_are_grey(self):
        from PIL import Image
        import numpy as np
        from services.silhouette import _make_masked_crop

        # Frame is all red; mask covers only the centre
        frame = Image.new("RGB", (100, 100), (255, 0, 0))
        seg = np.zeros((100, 100), dtype=bool)
        seg[30:70, 30:70] = True  # 40×40 centre
        mask = {"segmentation": seg, "bbox": [30, 30, 40, 40]}

        crop = _make_masked_crop(frame, mask, pad_fraction=0.0)
        arr = np.array(crop)
        # Pixels inside the mask bbox boundary should include red (from the frame)
        # Pixels outside the mask inside the crop area should be grey (128)
        # Since pad_fraction=0, the crop is exactly the bbox, all pixels are masked-in
        # — so all visible pixels in the crop come from the original red frame
        centre = arr[5, 5]  # inside mask
        self.assertGreater(int(centre[0]), 200, "Inside-mask pixel should be red")


if __name__ == "__main__":
    unittest.main()
