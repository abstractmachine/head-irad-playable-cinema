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
        self.assertEqual(len(fg), 3)
        self.assertEqual(len(bg), 3)
        for v in fg + bg:
            self.assertGreaterEqual(v, 0)
            self.assertLessEqual(v, 255)

    def test_border_vs_center_distinct_colors(self):
        """A red border and blue center should yield distinct fg and bg."""
        img = self._make_image(border_color=(220, 30, 30), center_color=(30, 30, 220))
        img_path = self.tmp / "contrast.png"
        img.save(str(img_path))

        fg, bg = extract_fg_bg(img_path)

        # Foreground (center) should be predominantly blue
        self.assertGreater(fg[2], fg[0], msg=f"Expected blue-dominant fg, got {fg}")
        # Background (border) should be predominantly red
        self.assertGreater(bg[0], bg[2], msg=f"Expected red-dominant bg, got {bg}")

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


if __name__ == "__main__":
    unittest.main()
