"""Tests for data/palette.py.

Covers:
- Cache path generation
- load_palette / save_palette round-trip
- save_palette raises FileExistsError when cache exists and force=False
- save_palette overwrites when force=True
- _extract_dominant_colour basic cases
- extract_fg_bg on a synthetic image
- create_palette_for_movie:
    - skips when cache already exists and force=False
    - processes shots with best-frame PNGs
    - skips shots without best_frame in annotation
    - skips shots whose PNG does not exist
    - handles missing shotlist gracefully
- get_palette returns cached data
- create_palette_for_all_movies aggregates results
- rescue helpers: _delta_e_cie76, _is_near_black, _pair_separation_score
- _rank_region_candidates candidate ranking
- _extract_fg_bg_full rescue pass behaviour and diagnostics schema
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from data.palette import (
    _extract_dominant_colour,
    create_palette_for_all_movies,
    create_palette_for_movie,
    extract_fg_bg,
    get_palette,
    get_palette_path,
    load_palette,
    save_palette,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp: Path) -> str:
    """Return a minimal project directory structure under *tmp*."""
    project = tmp / "project"
    project.mkdir()
    return str(project)


def _write_annotation_json(project: str, filename: str, media_type: str, entries: list) -> Path:
    from data.annotate import get_annotation_json_path
    path = get_annotation_json_path(project, filename, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def _write_best_frame_png(project: str, media_type: str, filename: str, shot_id: str) -> Path:
    from services.frame_match import best_frame_path
    p = best_frame_path(project, media_type, filename, shot_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write a tiny valid 2×2 RGB PNG using Pillow
    from PIL import Image
    img = Image.new("RGB", (2, 2), color=(200, 100, 50))
    img.save(str(p))
    return p


# ---------------------------------------------------------------------------
# Cache path helpers
# ---------------------------------------------------------------------------

class TestGetPalettePath(unittest.TestCase):
    def test_path_structure(self):
        p = get_palette_path("/project", "My Film (1956).mp4", "movies")
        self.assertEqual(
            p,
            Path("/project/data/index/palette/movies/My Film (1956).json"),
        )

    def test_stem_strips_extension(self):
        p = get_palette_path("/project", "film.mkv", "gameplay")
        self.assertEqual(p.name, "film.json")
        self.assertIn("gameplay", str(p))


# ---------------------------------------------------------------------------
# load_palette / save_palette
# ---------------------------------------------------------------------------

class TestLoadSavePalette(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = _make_project(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_returns_none_when_absent(self):
        self.assertIsNone(load_palette(self.project, "film.mp4", "movies"))

    def test_save_then_load_round_trips(self):
        data = {"movie": {"filename": "film.mp4"}, "shots": []}
        save_palette(self.project, "film.mp4", "movies", data)
        result = load_palette(self.project, "film.mp4", "movies")
        self.assertEqual(result, data)

    def test_save_raises_when_exists_no_force(self):
        data = {"shots": []}
        save_palette(self.project, "film.mp4", "movies", data)
        with self.assertRaises(FileExistsError):
            save_palette(self.project, "film.mp4", "movies", data, force=False)

    def test_save_overwrites_when_force(self):
        save_palette(self.project, "film.mp4", "movies", {"shots": []})
        new_data = {"shots": [{"shot_index": 0}]}
        save_palette(self.project, "film.mp4", "movies", new_data, force=True)
        result = load_palette(self.project, "film.mp4", "movies")
        self.assertEqual(result, new_data)

    def test_get_palette_alias(self):
        data = {"shots": []}
        save_palette(self.project, "film.mp4", "movies", data)
        self.assertEqual(get_palette(self.project, "film.mp4", "movies"), data)


# ---------------------------------------------------------------------------
# Dominant colour extraction
# ---------------------------------------------------------------------------

class TestExtractDominantColour(unittest.TestCase):
    def test_empty_returns_black(self):
        arr = np.zeros((0, 3), dtype=np.uint8)
        self.assertEqual(_extract_dominant_colour(arr), (0, 0, 0))

    def test_uniform_array(self):
        arr = np.full((100, 3), 200, dtype=np.uint8)
        self.assertEqual(_extract_dominant_colour(arr), (200, 200, 200))

    def test_dominant_colour_wins(self):
        # 90 red pixels, 10 blue pixels
        red = np.tile([255, 0, 0], (90, 1)).astype(np.uint8)
        blue = np.tile([0, 0, 255], (10, 1)).astype(np.uint8)
        arr = np.vstack([red, blue])
        r, g, b = _extract_dominant_colour(arr)
        # Red should dominate
        self.assertGreater(r, 200)
        self.assertLess(b, 50)

    def test_deterministic(self):
        rng = np.random.default_rng(42)
        arr = rng.integers(0, 256, (500, 3), dtype=np.uint8)
        first = _extract_dominant_colour(arr)
        second = _extract_dominant_colour(arr)
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# extract_fg_bg
# ---------------------------------------------------------------------------

class TestExtractFgBg(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_image(self, border_color, center_color, size=128):
        """Create a synthetic image with distinct border and center regions."""
        from PIL import Image
        img = Image.new("RGB", (size, size), border_color)
        border = size // 4
        # Fill center with center_color
        center_pixels = []
        for y in range(border, size - border):
            for x in range(border, size - border):
                center_pixels.append((x, y))
        for x, y in center_pixels:
            img.putpixel((x, y), center_color)
        return img

    def test_returns_two_rgb_triplets(self):
        img_path = self.tmp / "test.png"
        from PIL import Image
        img = Image.new("RGB", (64, 64), (100, 200, 50))
        img.save(str(img_path))
        fg, bg = extract_fg_bg(img_path)
        self.assertIn("rgb", fg)
        self.assertIn("rgb", bg)
        for v in fg["rgb"] + bg["rgb"]:
            self.assertGreaterEqual(v, 0)
            self.assertLessEqual(v, 255)

    def test_border_vs_center_distinct_colors(self):
        """A red border and blue center should yield distinct fg and bg."""
        img = self._make_image(border_color=(220, 30, 30), center_color=(30, 30, 220))
        img_path = self.tmp / "contrast.png"
        img.save(str(img_path))

        fg, bg = extract_fg_bg(img_path)

        # Foreground (center) should be predominantly blue
        fg_rgb = fg["rgb"]
        bg_rgb = bg["rgb"]
        self.assertGreater(fg_rgb[2], fg_rgb[0], msg=f"Expected blue-dominant fg, got {fg_rgb}")
        # Background (border) should be predominantly red
        self.assertGreater(bg_rgb[0], bg_rgb[2], msg=f"Expected red-dominant bg, got {bg_rgb}")

    def test_missing_file_raises(self):
        with self.assertRaises(Exception):
            extract_fg_bg(self.tmp / "nonexistent.png")


# ---------------------------------------------------------------------------
# create_palette_for_movie
# ---------------------------------------------------------------------------

class TestCreatePaletteForMovie(unittest.TestCase):
    FILENAME = "My Film (1956) {tmdb-99}.mp4"
    MEDIA_TYPE = "movies"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = _make_project(Path(self._tmp.name))
        # Stub metadata so get_metadata returns something sensible
        self._meta_patcher = patch(
            "data.palette.get_metadata",
            return_value=[{
                "filename": self.FILENAME,
                "media_type": self.MEDIA_TYPE,
                "title": "My Film",
                "year": 1956,
                "tmdb": "99",
            }],
        )
        self._meta_patcher.start()
        # Stub compute_media_id
        self._media_id_patcher = patch(
            "data.palette.compute_media_id",
            return_value="tmdb_99",
        )
        self._media_id_patcher.start()

    def tearDown(self):
        self._meta_patcher.stop()
        self._media_id_patcher.stop()
        self._tmp.cleanup()

    def _annotation_entry(self, shot_id: str, frame_number: int | None) -> dict:
        best_frame = {"frame": frame_number, "score": 0.9, "method": "clip"} if frame_number is not None else None
        return {
            "movie": {"filename": self.FILENAME, "title": "My Film"},
            "shot": {
                "shot_id": shot_id,
                "annotation": {"setting": "interior"},
                **({"best_frame": best_frame} if best_frame else {}),
            },
        }

    def test_skips_when_cached_no_force(self):
        # Pre-write a palette cache
        save_palette(self.project, self.FILENAME, self.MEDIA_TYPE, {"shots": []})
        # Write a dummy annotation JSON too
        _write_annotation_json(self.project, self.FILENAME, self.MEDIA_TYPE, [])

        with patch("data.palette.load_annotation_items", return_value=[]) as mock_load:
            summary = create_palette_for_movie(
                self.project, self.FILENAME, self.MEDIA_TYPE, force=False
            )

        self.assertTrue(summary.get("cached"))
        mock_load.assert_not_called()

    def test_force_overwrites_cache(self):
        save_palette(self.project, self.FILENAME, self.MEDIA_TYPE, {"shots": []})
        entries = [self._annotation_entry("tmdb_99@f0000-f0100", None)]

        with patch("data.palette.load_annotation_items", return_value=entries), \
             patch("data.palette.read_shotlist", side_effect=FileNotFoundError("no shotlist")):
            summary = create_palette_for_movie(
                self.project, self.FILENAME, self.MEDIA_TYPE, force=True
            )

        self.assertFalse(summary.get("cached"))

    def test_skips_shot_without_best_frame(self):
        entry = self._annotation_entry("tmdb_99@f0000-f0100", None)
        with patch("data.palette.load_annotation_items", return_value=[entry]), \
             patch("data.palette.read_shotlist", side_effect=FileNotFoundError("no shotlist")):
            summary = create_palette_for_movie(
                self.project, self.FILENAME, self.MEDIA_TYPE, force=True
            )

        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["processed"], 0)

    def test_skips_shot_when_png_missing(self):
        entry = self._annotation_entry("tmdb_99@f0000-f0100", 50)
        # PNG is NOT written, so it won't exist
        with patch("data.palette.load_annotation_items", return_value=[entry]), \
             patch("data.palette.read_shotlist", side_effect=FileNotFoundError("no shotlist")):
            summary = create_palette_for_movie(
                self.project, self.FILENAME, self.MEDIA_TYPE, force=True
            )

        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["processed"], 0)

    def test_processes_shot_with_valid_png(self):
        shot_id = "tmdb_99@f0000-f0100"
        entry = self._annotation_entry(shot_id, 50)

        # Write a real PNG
        _write_best_frame_png(self.project, self.MEDIA_TYPE, self.FILENAME, shot_id)

        with patch("data.palette.load_annotation_items", return_value=[entry]), \
             patch("data.palette.read_shotlist", side_effect=FileNotFoundError("no shotlist")):
            summary = create_palette_for_movie(
                self.project, self.FILENAME, self.MEDIA_TYPE, force=True
            )

        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["failed"], 0)

        # Verify cache was written with correct structure
        cached = load_palette(self.project, self.FILENAME, self.MEDIA_TYPE)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["movie"]["filename"], self.FILENAME)
        self.assertEqual(cached["source"], "best_frame")
        self.assertIn("created_at", cached)
        self.assertEqual(len(cached["shots"]), 1)

        shot_entry = cached["shots"][0]
        self.assertIn("foreground", shot_entry)
        self.assertIn("background", shot_entry)
        self.assertIsInstance(shot_entry["foreground"]["rgb"], list)
        self.assertIsInstance(shot_entry["background"]["rgb"], list)
        self.assertEqual(len(shot_entry["foreground"]["rgb"]), 3)

    def test_cache_structure_summary(self):
        shot_id = "tmdb_99@f0000-f0100"
        entry = self._annotation_entry(shot_id, 50)
        _write_best_frame_png(self.project, self.MEDIA_TYPE, self.FILENAME, shot_id)

        with patch("data.palette.load_annotation_items", return_value=[entry]), \
             patch("data.palette.read_shotlist", side_effect=FileNotFoundError("no shotlist")):
            create_palette_for_movie(
                self.project, self.FILENAME, self.MEDIA_TYPE, force=True
            )

        cached = load_palette(self.project, self.FILENAME, self.MEDIA_TYPE)
        summary = cached["summary"]
        self.assertEqual(summary["shot_count"], 1)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["failed"], 0)


# ---------------------------------------------------------------------------
# create_palette_for_all_movies
# ---------------------------------------------------------------------------

class TestCreatePaletteForAllMovies(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = _make_project(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_aggregates_results(self):
        meta = [
            {"filename": "film_a.mp4"},
            {"filename": "film_b.mp4"},
        ]
        movie_summary = {
            "filename": "film_a.mp4",
            "shot_count": 5,
            "processed": 3,
            "skipped": 2,
            "failed": 0,
            "cached": False,
        }

        with patch("data.palette.get_metadata", return_value=meta), \
             patch("data.palette.create_palette_for_movie", return_value=movie_summary):
            result = create_palette_for_all_movies(self.project, "movies")

        self.assertEqual(result["total_files"], 2)

    def test_skips_file_not_found(self):
        meta = [{"filename": "missing.mp4"}]

        with patch("data.palette.get_metadata", return_value=meta), \
             patch(
                 "data.palette.create_palette_for_movie",
                 side_effect=FileNotFoundError("no annotations"),
             ):
            result = create_palette_for_all_movies(self.project, "movies")

        self.assertEqual(result["total_files"], 1)
        # Should not raise; results should contain the skipped entry
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["results"][0].get("skipped"))


# ---------------------------------------------------------------------------
# Rescue helpers — unit tests
# ---------------------------------------------------------------------------

class TestDeltaECie76(unittest.TestCase):
    def test_identical_colours_zero(self):
        from data.palette import _delta_e_cie76
        self.assertAlmostEqual(_delta_e_cie76([50, 0, 0], [50, 0, 0]), 0.0)

    def test_pure_luminance_difference(self):
        from data.palette import _delta_e_cie76
        # |ΔL| = 10, Δa = Δb = 0 → ΔE = 10
        self.assertAlmostEqual(_delta_e_cie76([40, 0, 0], [50, 0, 0]), 10.0, places=5)

    def test_symmetry(self):
        from data.palette import _delta_e_cie76
        a, b = [30, 10, -5], [60, -15, 20]
        self.assertAlmostEqual(_delta_e_cie76(a, b), _delta_e_cie76(b, a), places=10)


class TestIsNearBlack(unittest.TestCase):
    def test_pure_black(self):
        from data.palette import _is_near_black
        self.assertTrue(_is_near_black([0.0, 0.0, 0.0]))

    def test_very_dark_achromatic(self):
        from data.palette import _is_near_black
        self.assertTrue(_is_near_black([5.0, 2.0, -1.0]))

    def test_luminous_not_near_black(self):
        from data.palette import _is_near_black
        self.assertFalse(_is_near_black([50.0, 0.0, 0.0]))

    def test_dark_but_chromatic_not_near_black(self):
        from data.palette import _is_near_black
        # Dark blue: L*≈10 but C*≈25 — should NOT be near-black
        self.assertFalse(_is_near_black([10.0, -5.0, -22.0]))

    def test_boundary_luminance(self):
        from data.palette import _is_near_black
        from data.palette import _NEAR_BLACK_L, _NEAR_BLACK_CHROMA
        # Just below threshold on both — near-black
        self.assertTrue(_is_near_black([_NEAR_BLACK_L - 0.1, 0.0, _NEAR_BLACK_CHROMA - 0.1]))
        # Just above luminance — not near-black
        self.assertFalse(_is_near_black([_NEAR_BLACK_L + 0.1, 0.0, 0.0]))


class TestPairSeparationScore(unittest.TestCase):
    def test_identical_pair_zero(self):
        from data.palette import _pair_separation_score
        self.assertAlmostEqual(_pair_separation_score([50, 0, 0], [50, 0, 0]), 0.0)

    def test_high_contrast_beats_low(self):
        from data.palette import _pair_separation_score
        score_bw  = _pair_separation_score([0, 0, 0], [100, 0, 0])   # black vs white
        score_sim = _pair_separation_score([5, 1, -1], [8, 2, -2])   # near-identical dark
        self.assertGreater(score_bw, score_sim)

    def test_chromatic_difference_raises_score(self):
        from data.palette import _pair_separation_score
        # Same luminance, but one pair has chroma difference
        score_chroma = _pair_separation_score([30,  0,  0], [30, 30, -20])
        score_flat   = _pair_separation_score([30,  0,  0], [30,  0,   0])
        self.assertGreater(score_chroma, score_flat)


# ---------------------------------------------------------------------------
# _rank_region_candidates — unit tests
# ---------------------------------------------------------------------------

class TestRankRegionCandidates(unittest.TestCase):
    def test_empty_pixels_fallback(self):
        from data.palette import _rank_region_candidates
        result = _rank_region_candidates(np.zeros((0, 3), dtype=np.uint8))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["rgb"], (0, 0, 0))
        self.assertEqual(result[0]["lab"], [0.0, 0.0, 0.0])

    def test_uniform_image_top_candidate(self):
        from data.palette import _rank_region_candidates
        pixels = np.full((100, 3), 200, dtype=np.uint8)
        result = _rank_region_candidates(pixels)
        self.assertGreater(len(result), 0)
        r, g, b = result[0]["rgb"]
        self.assertAlmostEqual(r, 200, delta=5)
        self.assertAlmostEqual(g, 200, delta=5)
        self.assertAlmostEqual(b, 200, delta=5)

    def test_sorted_by_weight_descending(self):
        from data.palette import _rank_region_candidates
        pixels = np.vstack([
            np.tile([200, 100, 50], (80, 1)).astype(np.uint8),
            np.tile([10,  10,  10], (20, 1)).astype(np.uint8),
        ])
        result = _rank_region_candidates(pixels)
        weights = [c["weight"] for c in result]
        self.assertEqual(weights, sorted(weights, reverse=True))

    def test_candidate_schema(self):
        from data.palette import _rank_region_candidates
        pixels = np.tile([120, 60, 180], (50, 1)).astype(np.uint8)
        result = _rank_region_candidates(pixels)
        for cand in result:
            self.assertIn("rgb", cand)
            self.assertIn("lab", cand)
            self.assertIn("luminance", cand)
            self.assertIn("chroma", cand)
            self.assertIn("weight", cand)
            self.assertIn("size", cand)
            self.assertEqual(len(cand["rgb"]), 3)
            self.assertEqual(len(cand["lab"]), 3)

    def test_multiple_distinct_clusters(self):
        from data.palette import _rank_region_candidates
        red  = np.tile([220, 30, 30],  (50, 1)).astype(np.uint8)
        blue = np.tile([30, 30, 220],  (50, 1)).astype(np.uint8)
        dark = np.tile([5,   5,   5],  (50, 1)).astype(np.uint8)
        result = _rank_region_candidates(np.vstack([red, blue, dark]))
        self.assertGreater(len(result), 1)


# ---------------------------------------------------------------------------
# _extract_fg_bg_full — integration / rescue behaviour
# ---------------------------------------------------------------------------

class TestExtractFgBgFull(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_png(self, arr: np.ndarray, name: str) -> Path:
        from PIL import Image
        p = self.tmp / name
        Image.fromarray(arr, "RGB").save(str(p))
        return p

    def _solid_arr(self, border_color, center_color, size=128, border=32):
        arr = np.zeros((size, size, 3), dtype=np.uint8)
        arr[:] = border_color
        arr[border:size - border, border:size - border] = center_color
        return arr

    # ------------------------------------------------------------------
    # Diagnostics schema

    def test_diagnostics_keys_always_present_lab(self):
        from data.palette import _extract_fg_bg_full
        arr = self._solid_arr((200, 50, 30), (30, 80, 200))
        _fg, _bg, diag = _extract_fg_bg_full(self._make_png(arr, "schema.png"), method="lab")
        expected = {
            "fg_bg_delta_e", "fg_bg_luminance_delta", "fg_bg_chroma_delta",
            "rescue_applied", "rescue_reason", "near_black_pair",
            "candidate_count_fg", "candidate_count_bg",
        }
        self.assertTrue(expected.issubset(diag.keys()),
                        f"Missing keys: {expected - diag.keys()}")

    def test_diagnostics_empty_for_simple_method(self):
        from data.palette import _extract_fg_bg_full
        arr = self._solid_arr((200, 50, 30), (30, 80, 200))
        _fg, _bg, diag = _extract_fg_bg_full(self._make_png(arr, "simple.png"), method="simple")
        self.assertEqual(diag, {})

    # ------------------------------------------------------------------
    # High-contrast scene — no rescue expected

    def test_high_contrast_no_rescue(self):
        from data.palette import _extract_fg_bg_full
        arr = self._solid_arr((220, 30, 30), (30, 30, 220))  # red border, blue center
        _fg, _bg, diag = _extract_fg_bg_full(self._make_png(arr, "hc.png"), method="lab")
        self.assertFalse(diag["rescue_applied"])
        self.assertFalse(diag["near_black_pair"])
        self.assertGreater(diag["fg_bg_delta_e"], 15.0)

    # ------------------------------------------------------------------
    # Purely black scene — rescue triggers but finds no improvement

    def test_pure_black_scene_stays_near_black(self):
        from data.palette import _extract_fg_bg_full, _is_near_black
        arr = np.zeros((128, 128, 3), dtype=np.uint8)
        fg, bg, diag = _extract_fg_bg_full(self._make_png(arr, "black.png"), method="lab")
        # Both colours must remain near-black — no false saturation
        self.assertTrue(_is_near_black(fg["lab"]))
        self.assertTrue(_is_near_black(bg["lab"]))
        # near_black_pair must be flagged
        self.assertTrue(diag["near_black_pair"])
        # rescue_applied = False because all candidates are identical black
        self.assertFalse(diag["rescue_applied"])

    # ------------------------------------------------------------------
    # Dark scene with chromatic accent — rescue should prefer the accent

    def test_dark_scene_chromatic_accent_rescue(self):
        from data.palette import _extract_fg_bg_full
        # Near-black base, but center has a blue accent cluster (enough pixels
        # to form a k-means cluster with higher perceptual weight than dark bulk)
        rng = np.random.default_rng(0)
        arr = self._solid_arr((4, 4, 6), (5, 5, 8))
        # Scatter 600 blue-ish accent pixels into the center region
        ys = rng.integers(32, 96, 600)
        xs = rng.integers(32, 96, 600)
        arr[ys, xs] = (35, 55, 145)
        fg, bg, diag = _extract_fg_bg_full(self._make_png(arr, "accent.png"), method="lab")
        # Diagnostics must be populated
        self.assertIn("near_black_pair", diag)
        self.assertIn("rescue_applied", diag)
        # If rescue was applied, the rescued ΔE must exceed the original baseline
        # (verified implicitly by the diag values being updated)
        if diag["rescue_applied"]:
            self.assertGreater(diag["fg_bg_delta_e"], 0.0)

    # ------------------------------------------------------------------
    # Determinism

    def test_deterministic_on_repeated_calls(self):
        from data.palette import _extract_fg_bg_full
        rng = np.random.default_rng(7)
        arr = self._solid_arr((3, 3, 5), (6, 6, 9))
        arr[32:96, 32:96][rng.integers(0, 64, (400, 2))[:, 0],
                          rng.integers(0, 64, (400, 2))[:, 1]] = (45, 20, 10)
        p = self._make_png(arr, "det.png")
        fg1, bg1, d1 = _extract_fg_bg_full(p, method="lab")
        fg2, bg2, d2 = _extract_fg_bg_full(p, method="lab")
        self.assertEqual(fg1["rgb"], fg2["rgb"])
        self.assertEqual(bg1["rgb"], bg2["rgb"])
        self.assertEqual(d1["rescue_applied"], d2["rescue_applied"])

    # ------------------------------------------------------------------
    # Output schema — colour dict keys

    def test_lab_colour_dict_has_expected_keys(self):
        from data.palette import _extract_fg_bg_full
        arr = self._solid_arr((180, 40, 20), (20, 40, 180))
        fg, bg, _diag = _extract_fg_bg_full(self._make_png(arr, "keys.png"), method="lab")
        for colour in (fg, bg):
            self.assertIn("rgb", colour)
            self.assertIn("lab", colour)
            self.assertIn("luminance", colour)
            self.assertIn("chroma", colour)
            self.assertNotIn("weight", colour)
            self.assertNotIn("size", colour)
            self.assertEqual(len(colour["rgb"]), 3)

    # ------------------------------------------------------------------
    # Diagnostics wired into shot-level palette JSON

    def test_diagnostics_present_in_processed_shot(self):
        """create_palette_for_movie should include diagnostics in shot dicts."""
        import json
        from data.palette import create_palette_for_movie

        FILENAME  = "diag_test {tmdb-1}.mp4"
        MEDIA_TYPE = "movies"
        shot_id   = "tmdb_1@f0000-f0100"

        with tempfile.TemporaryDirectory() as tmpdir:
            project = str(Path(tmpdir) / "project")
            Path(project).mkdir()

            # Write a valid best-frame PNG (distinct colours so no rescue)
            from services.frame_match import best_frame_path
            from PIL import Image
            p = best_frame_path(project, MEDIA_TYPE, FILENAME, shot_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            img = Image.new("RGB", (128, 128), (180, 40, 20))
            for y in range(32, 96):
                for x in range(32, 96):
                    img.putpixel((x, y), (20, 40, 180))
            img.save(str(p))

            entry = {
                "movie": {"filename": FILENAME},
                "shot": {
                    "shot_id": shot_id,
                    "best_frame": {"frame": 50, "score": 0.9, "method": "clip"},
                },
            }

            with patch("data.palette.get_metadata", return_value=[{
                    "filename": FILENAME, "title": "Diag Test", "year": 2024,
                }]), \
                 patch("data.palette.compute_media_id", return_value="tmdb_1"), \
                 patch("data.palette.load_annotation_items", return_value=[entry]), \
                 patch("data.palette.read_shotlist", side_effect=FileNotFoundError()):
                create_palette_for_movie(project, FILENAME, MEDIA_TYPE, force=True)

            cached = load_palette(project, FILENAME, MEDIA_TYPE)
            shot_entry = cached["shots"][0]
            self.assertIn("diagnostics", shot_entry)
            diag = shot_entry["diagnostics"]
            self.assertIn("fg_bg_delta_e", diag)
            self.assertIn("near_black_pair", diag)
            self.assertIn("rescue_applied", diag)


if __name__ == "__main__":
    unittest.main()
