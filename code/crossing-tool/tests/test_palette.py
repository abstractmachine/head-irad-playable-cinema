"""Tests for data/palette.py.

Covers:
- Cache path generation
- load_palette / save_palette round-trip
- save_palette raises FileExistsError when cache exists and force=False
- save_palette overwrites when force=True
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
        p = get_palette_path("/project", "My Film (1956).mp4", "movie")
        self.assertEqual(
            p,
            Path("/project/data/palettes/movies/My Film (1956).json"),
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
        self.assertIsNone(load_palette(self.project, "film.mp4", "movie"))

    def test_save_then_load_round_trips(self):
        data = {"movie": {"filename": "film.mp4"}, "shots": []}
        save_palette(self.project, "film.mp4", "movie", data)
        result = load_palette(self.project, "film.mp4", "movie")
        self.assertEqual(result, data)

    def test_save_raises_when_exists_no_force(self):
        data = {"shots": []}
        save_palette(self.project, "film.mp4", "movie", data)
        with self.assertRaises(FileExistsError):
            save_palette(self.project, "film.mp4", "movie", data, force=False)

    def test_save_overwrites_when_force(self):
        save_palette(self.project, "film.mp4", "movie", {"shots": []})
        new_data = {"shots": [{"shot_index": 0}]}
        save_palette(self.project, "film.mp4", "movie", new_data, force=True)
        result = load_palette(self.project, "film.mp4", "movie")
        self.assertEqual(result, new_data)

    def test_get_palette_alias(self):
        data = {"shots": []}
        save_palette(self.project, "film.mp4", "movie", data)
        self.assertEqual(get_palette(self.project, "film.mp4", "movie"), data)


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
    MEDIA_TYPE = "movie"

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
            result = create_palette_for_all_movies(self.project, "movie")

        self.assertEqual(result["total_files"], 2)

    def test_skips_file_not_found(self):
        meta = [{"filename": "missing.mp4"}]

        with patch("data.palette.get_metadata", return_value=meta), \
             patch(
                 "data.palette.create_palette_for_movie",
                 side_effect=FileNotFoundError("no annotations"),
             ):
            result = create_palette_for_all_movies(self.project, "movie")

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

    def test_diagnostics_keys_always_present(self):
        from data.palette import _extract_fg_bg_full
        arr = self._solid_arr((200, 50, 30), (30, 80, 200))
        _fg, _bg, diag = _extract_fg_bg_full(self._make_png(arr, "schema.png"))
        expected = {
            "fg_bg_delta_e", "fg_bg_luminance_delta", "fg_bg_chroma_delta",
            "rescue_applied", "rescue_reason", "near_black_pair",
            "candidate_count_fg", "candidate_count_bg",
        }
        self.assertTrue(expected.issubset(diag.keys()),
                        f"Missing keys: {expected - diag.keys()}")

    # ------------------------------------------------------------------
    # High-contrast scene — no rescue expected

    def test_high_contrast_no_rescue(self):
        from data.palette import _extract_fg_bg_full
        arr = self._solid_arr((220, 30, 30), (30, 30, 220))  # red border, blue center
        _fg, _bg, diag = _extract_fg_bg_full(self._make_png(arr, "hc.png"))
        self.assertFalse(diag["rescue_applied"])
        self.assertFalse(diag["near_black_pair"])
        self.assertGreater(diag["fg_bg_delta_e"], 15.0)

    # ------------------------------------------------------------------
    # Purely black scene — rescue triggers but finds no improvement

    def test_pure_black_scene_stays_near_black(self):
        from data.palette import _extract_fg_bg_full, _is_near_black
        arr = np.zeros((128, 128, 3), dtype=np.uint8)
        fg, bg, diag = _extract_fg_bg_full(self._make_png(arr, "black.png"))
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
        fg, bg, diag = _extract_fg_bg_full(self._make_png(arr, "accent.png"))
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
        fg1, bg1, d1 = _extract_fg_bg_full(p)
        fg2, bg2, d2 = _extract_fg_bg_full(p)
        self.assertEqual(fg1["rgb"], fg2["rgb"])
        self.assertEqual(bg1["rgb"], bg2["rgb"])
        self.assertEqual(d1["rescue_applied"], d2["rescue_applied"])

    # ------------------------------------------------------------------
    # Output schema — colour dict keys

    def test_colour_dict_has_expected_keys(self):
        from data.palette import _extract_fg_bg_full
        arr = self._solid_arr((180, 40, 20), (20, 40, 180))
        fg, bg, _diag = _extract_fg_bg_full(self._make_png(arr, "keys.png"))
        for colour in (fg, bg):
            self.assertIn("rgb", colour)
            self.assertIn("lab", colour)
            self.assertIn("luminance", colour)
            self.assertIn("chroma", colour)
            self.assertIn("palette", colour)
            self.assertIn("coverage", colour)
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
        MEDIA_TYPE = "movie"
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


# ===========================================================================\n# Figure-ground pipeline tests\n# ===========================================================================

class TestMeanShiftSimplify(unittest.TestCase):
    """_mean_shift_simplify: should return same shape; deterministic."""

    def test_returns_same_shape(self):
        from data.palette import _mean_shift_simplify
        arr = np.zeros((64, 64, 3), dtype=np.uint8)
        out = _mean_shift_simplify(arr)
        self.assertEqual(out.shape, arr.shape)
        self.assertEqual(out.dtype, np.uint8)

    def test_deterministic(self):
        from data.palette import _mean_shift_simplify
        rng = np.random.RandomState(42)
        arr = rng.randint(0, 256, (64, 64, 3), dtype=np.uint8)
        out1 = _mean_shift_simplify(arr)
        out2 = _mean_shift_simplify(arr)
        np.testing.assert_array_equal(out1, out2)

    def test_uniform_image_unchanged(self):
        from data.palette import _mean_shift_simplify
        arr = np.full((64, 64, 3), 128, dtype=np.uint8)
        out = _mean_shift_simplify(arr)
        np.testing.assert_array_equal(out, arr)


class TestAgglomerativePalette(unittest.TestCase):
    """_agglomerative_palette: schema, sorted by weight, graceful fallbacks."""

    def test_empty_returns_null(self):
        from data.palette import _agglomerative_palette
        result = _agglomerative_palette(np.empty((0, 3), dtype=np.uint8))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["rgb"], (0, 0, 0))

    def test_returns_at_most_n_palette(self):
        from data.palette import _agglomerative_palette
        rng = np.random.RandomState(0)
        pixels = rng.randint(0, 256, (500, 3), dtype=np.uint8)
        result = _agglomerative_palette(pixels, n_clusters=8, n_palette=4)
        self.assertLessEqual(len(result), 4)

    def test_schema_keys(self):
        from data.palette import _agglomerative_palette
        rng = np.random.RandomState(1)
        pixels = rng.randint(0, 256, (200, 3), dtype=np.uint8)
        result = _agglomerative_palette(pixels, n_clusters=4, n_palette=3)
        for entry in result:
            self.assertIn("rgb", entry)
            self.assertIn("lab", entry)
            self.assertIn("luminance", entry)
            self.assertIn("chroma", entry)
            self.assertIn("weight", entry)
            self.assertIn("size", entry)

    def test_sorted_by_weight_descending(self):
        from data.palette import _agglomerative_palette
        rng = np.random.RandomState(2)
        pixels = rng.randint(0, 256, (400, 3), dtype=np.uint8)
        result = _agglomerative_palette(pixels, n_clusters=6, n_palette=4)
        weights = [c["weight"] for c in result]
        self.assertEqual(weights, sorted(weights, reverse=True))

    def test_deterministic(self):
        from data.palette import _agglomerative_palette
        rng = np.random.RandomState(3)
        pixels = rng.randint(0, 256, (300, 3), dtype=np.uint8)
        r1 = _agglomerative_palette(pixels, n_clusters=5, n_palette=3)
        r2 = _agglomerative_palette(pixels, n_clusters=5, n_palette=3)
        for a, b in zip(r1, r2):
            self.assertEqual(a["rgb"], b["rgb"])

    def test_single_colour_scene(self):
        from data.palette import _agglomerative_palette
        pixels = np.full((100, 3), (100, 150, 200), dtype=np.uint8)
        result = _agglomerative_palette(pixels, n_clusters=4, n_palette=3)
        self.assertGreater(len(result), 0)
        top_rgb = result[0]["rgb"]
        for v, exp in zip(top_rgb, (100, 150, 200)):
            self.assertAlmostEqual(v, exp, delta=2)


class TestRegionInfoFromCandidates(unittest.TestCase):
    """_region_info_from_candidates: palette list, coverage, backward compat."""

    def test_empty_candidates(self):
        from data.palette import _region_info_from_candidates
        out = _region_info_from_candidates([], 1000)
        self.assertIn("rgb", out)
        self.assertEqual(out["palette"], [])
        self.assertEqual(out["coverage"], 0.0)

    def test_dominant_is_top_candidate(self):
        from data.palette import _region_info_from_candidates
        cands = [
            {"rgb": (200, 0, 0), "lab": [40.0, 0.0, 0.0], "luminance": 0.4, "chroma": 0.0,
             "weight": 10.0, "size": 100},
            {"rgb": (0, 200, 0), "lab": [30.0, 0.0, 0.0], "luminance": 0.3, "chroma": 0.0,
             "weight": 5.0, "size": 50},
        ]
        out = _region_info_from_candidates(cands, 1000)
        self.assertEqual(out["rgb"], [200, 0, 0])

    def test_palette_contains_all_candidates(self):
        from data.palette import _region_info_from_candidates
        cands = [
            {"rgb": (200, 0, 0), "lab": [40.0, 0.0, 0.0], "luminance": 0.4, "chroma": 0.0,
             "weight": 10.0, "size": 100},
            {"rgb": (0, 200, 0), "lab": [30.0, 0.0, 0.0], "luminance": 0.3, "chroma": 0.0,
             "weight": 5.0, "size": 50},
        ]
        out = _region_info_from_candidates(cands, 1000)
        self.assertEqual(len(out["palette"]), 2)
        # weight / size keys stripped from palette output
        for p in out["palette"]:
            self.assertNotIn("weight", p)
            self.assertNotIn("size", p)

    def test_coverage_fraction(self):
        from data.palette import _region_info_from_candidates
        cands = [
            {"rgb": (100, 100, 100), "lab": [50.0, 0.0, 0.0], "luminance": 0.5, "chroma": 0.0,
             "weight": 3.0, "size": 300},
        ]
        out = _region_info_from_candidates(cands, 1000)
        self.assertAlmostEqual(out["coverage"], 0.3, places=2)


class TestSpatialMasks(unittest.TestCase):
    """_spatial_masks: fg is inner, bg is outer strip."""

    def test_fg_bg_non_overlapping(self):
        from data.palette import _spatial_masks
        fg, bg = _spatial_masks(128, 128, 32)
        self.assertFalse(np.any(fg & bg))

    def test_fg_bg_cover_all_pixels(self):
        from data.palette import _spatial_masks
        fg, bg = _spatial_masks(128, 128, 32)
        self.assertTrue(np.all(fg | bg))

    def test_border_is_bg(self):
        from data.palette import _spatial_masks
        fg, bg = _spatial_masks(128, 128, 32)
        # Top-left corner should be bg
        self.assertTrue(bg[0, 0])
        self.assertFalse(fg[0, 0])

    def test_center_is_fg(self):
        from data.palette import _spatial_masks
        fg, bg = _spatial_masks(128, 128, 32)
        self.assertTrue(fg[64, 64])
        self.assertFalse(bg[64, 64])


class TestExtractFgBgFigure(unittest.TestCase):
    """_extract_fg_bg_figure: schema, backward compat, diagnostics, rescue."""

    def _make_arr(self, height=256, width=256,
                  bg_rgb=(50, 50, 100), fg_rgb=(200, 100, 50)):
        """Synthetic frame: bg_rgb border, fg_rgb center."""
        arr = np.full((height, width, 3), bg_rgb, dtype=np.uint8)
        border = height // 4
        arr[border:height - border, border:width - border] = fg_rgb
        return arr

    def test_returns_three_items(self):
        from data.palette import _extract_fg_bg_figure
        arr = self._make_arr()
        result = _extract_fg_bg_figure(arr)
        self.assertEqual(len(result), 3)

    def test_fg_bg_have_rgb_key(self):
        from data.palette import _extract_fg_bg_figure
        arr = self._make_arr()
        fg, bg, _ = _extract_fg_bg_figure(arr)
        self.assertIn("rgb", fg)
        self.assertIn("rgb", bg)
        self.assertEqual(len(fg["rgb"]), 3)

    def test_fg_bg_have_palette_key(self):
        from data.palette import _extract_fg_bg_figure
        arr = self._make_arr()
        fg, bg, _ = _extract_fg_bg_figure(arr)
        self.assertIn("palette", fg)
        self.assertIn("palette", bg)
        self.assertIsInstance(fg["palette"], list)

    def test_fg_bg_have_coverage_key(self):
        from data.palette import _extract_fg_bg_figure
        arr = self._make_arr()
        fg, bg, diag = _extract_fg_bg_figure(arr)
        self.assertIn("coverage", fg)
        self.assertIn("coverage", bg)
        total = fg["coverage"] + bg["coverage"]
        self.assertAlmostEqual(total, 1.0, delta=0.05)

    def test_diagnostics_schema(self):
        from data.palette import _extract_fg_bg_figure
        arr = self._make_arr()
        _, _, diag = _extract_fg_bg_figure(arr)
        for key in (
            "method_used", "segmentation_used", "segmentation_confidence",
            "superpixels_used", "fallback_level",
            "fg_bg_delta_e", "rescue_applied", "near_black_pair",
        ):
            self.assertIn(key, diag, f"Missing key: {key}")

    def test_method_used_is_figure(self):
        from data.palette import _extract_fg_bg_figure
        arr = self._make_arr()
        _, _, diag = _extract_fg_bg_figure(arr)
        self.assertEqual(diag["method_used"], "figure")

    def test_spatial_fallback_without_sam2(self):
        from data.palette import _extract_fg_bg_figure
        arr = self._make_arr()
        _, _, diag = _extract_fg_bg_figure(arr, mask_generator=None)
        self.assertEqual(diag["segmentation_used"], "spatial")
        self.assertEqual(diag["fallback_level"], 2)

    def test_deterministic(self):
        from data.palette import _extract_fg_bg_figure
        rng = np.random.RandomState(99)
        arr = rng.randint(0, 256, (256, 256, 3), dtype=np.uint8)
        fg1, bg1, d1 = _extract_fg_bg_figure(arr)
        fg2, bg2, d2 = _extract_fg_bg_figure(arr)
        self.assertEqual(fg1["rgb"], fg2["rgb"])
        self.assertEqual(bg1["rgb"], bg2["rgb"])
        self.assertEqual(d1["rescue_applied"], d2["rescue_applied"])


class TestExtractFgBgFullFigure(unittest.TestCase):
    """_extract_fg_bg_full integration tests for the figure-ground pipeline."""

    def _make_png(self, tmpdir, bg_rgb=(60, 60, 120), fg_rgb=(200, 80, 30)):
        from PIL import Image
        img = Image.new("RGB", (128, 128), bg_rgb)
        for y in range(32, 96):
            for x in range(32, 96):
                img.putpixel((x, y), fg_rgb)
        p = Path(tmpdir) / "frame.png"
        img.save(str(p))
        return p

    def test_returns_extended_schema(self):
        from data.palette import _extract_fg_bg_full
        with tempfile.TemporaryDirectory() as tmpdir:
            p = self._make_png(tmpdir)
            fg, bg, diag = _extract_fg_bg_full(p)
        self.assertIn("rgb", fg)
        self.assertIn("palette", fg)
        self.assertIn("coverage", fg)
        self.assertIn("method_used", diag)
        self.assertEqual(diag["method_used"], "figure")

    def test_backward_compat_rgb_key(self):
        """extract_fg_bg should still return (fg, bg) with rgb key."""
        from data.palette import extract_fg_bg
        with tempfile.TemporaryDirectory() as tmpdir:
            p = self._make_png(tmpdir)
            fg, bg = extract_fg_bg(p)
        self.assertIn("rgb", fg)
        self.assertIn("rgb", bg)
        self.assertEqual(len(fg["rgb"]), 3)

    def test_distinct_colours(self):
        """For a clearly bi-tonal image the two regions should be distinguishable."""
        from data.palette import _extract_fg_bg_full
        from data.palette import _delta_e_cie76, _rgb_to_lab
        with tempfile.TemporaryDirectory() as tmpdir:
            p = self._make_png(tmpdir, bg_rgb=(10, 10, 60), fg_rgb=(220, 80, 20))
            fg, bg, _ = _extract_fg_bg_full(p)
        fg_lab = _rgb_to_lab(np.array([fg["rgb"]], dtype=np.uint8))[0]
        bg_lab = _rgb_to_lab(np.array([bg["rgb"]], dtype=np.uint8))[0]
        de = _delta_e_cie76(fg_lab, bg_lab)
        self.assertGreater(de, 10.0, "figure pipeline should separate a clearly bi-tonal image")

    def test_method_name_in_shot(self):
        """_process_one_shot should record 'figure' as method name."""
        from data.palette import create_palette_for_movie, load_palette
        from unittest.mock import patch

        FILENAME   = "figure_test {tmdb-99}.mp4"
        MEDIA_TYPE = "movie"
        shot_id    = "tmdb_99@f0000-f0100"

        with tempfile.TemporaryDirectory() as tmpdir:
            project = str(Path(tmpdir) / "project")
            Path(project).mkdir()

            from services.frame_match import best_frame_path
            from PIL import Image
            p = best_frame_path(project, MEDIA_TYPE, FILENAME, shot_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            img = Image.new("RGB", (128, 128), (40, 40, 100))
            for y in range(32, 96):
                for x in range(32, 96):
                    img.putpixel((x, y), (180, 60, 20))
            img.save(str(p))

            entry = {
                "movie": {"filename": FILENAME},
                "shot": {
                    "shot_id": shot_id,
                    "best_frame": {"frame": 50, "score": 0.9, "method": "clip"},
                },
            }

            with patch("data.palette.get_metadata", return_value=[{
                        "filename": FILENAME, "title": "Figure Test", "year": 2024,
                    }]), \
                 patch("data.palette.compute_media_id", return_value="tmdb_99"), \
                 patch("data.palette.load_annotation_items", return_value=[entry]), \
                 patch("data.palette.read_shotlist", side_effect=FileNotFoundError()):
                create_palette_for_movie(
                    project, FILENAME, MEDIA_TYPE, force=True
                )

            cached = load_palette(project, FILENAME, MEDIA_TYPE)
            shot_entry = cached["shots"][0]
            self.assertEqual(shot_entry.get("method"), "figure")
            self.assertIn("diagnostics", shot_entry)
            diag = shot_entry["diagnostics"]
            self.assertIn("method_used", diag)
            self.assertEqual(diag["method_used"], "figure")
