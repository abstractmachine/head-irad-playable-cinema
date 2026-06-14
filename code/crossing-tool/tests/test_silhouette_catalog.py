"""Tests for services/silhouette_catalog.py.

Covers:
- Path helpers (catalog_base_dir, catalog_item_dir, _safe_label)
- PNG filename helpers (_object_png_name, _object_json_name, _next_object_index)
- Mask quality filters (_passes_quality_filters, _touches_border)
- IoU computation and deduplication (_masks_iou, _deduplicate_masks)
- extract_object_png (transparent RGBA output shape and alpha channel)
- audit_catalog (empty and populated catalog)
- clear_catalog (targeted and full clear, dry-run mode)
- scan_catalog (directory traversal)
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image

from services.silhouette_catalog import (
    CATALOG_VERSION,
    _safe_label,
    _next_object_index,
    _object_png_name,
    _object_json_name,
    _passes_quality_filters,
    _touches_border,
    _masks_iou,
    _deduplicate_masks,
    catalog_base_dir,
    catalog_item_dir,
    extract_object_png,
    scan_catalog,
    audit_catalog,
    clear_catalog,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mask_dict(height=100, width=100, area_fraction=0.1, x=20, y=20, w=30, h=30):
    """Return a minimal SAM-style mask dict with a filled rectangular segmentation."""
    seg = np.zeros((height, width), dtype=bool)
    seg[y:y + h, x:x + w] = True
    area = int(seg.sum())
    return {
        "segmentation": seg,
        "area": area,
        "bbox": [x, y, w, h],
    }


def _make_catalog_entry(label_dir: Path, index: int, shot_id: str = "tmdb_1@f0-f100") -> Path:
    """Write a minimal object PNG + JSON pair into *label_dir*."""
    label_dir.mkdir(parents=True, exist_ok=True)

    png_path  = label_dir / _object_png_name(index)
    json_path = label_dir / _object_json_name(index)

    # 10×10 RGBA PNG
    img = Image.fromarray(np.zeros((10, 10, 4), dtype=np.uint8), mode="RGBA")
    img.save(str(png_path), format="PNG")

    meta = {
        "schema_version": CATALOG_VERSION,
        "label":          label_dir.name,
        "shot_id":        shot_id,
        "filename":       "film.mkv",
        "filename_stem":  label_dir.parent.name,
        "media_type":     "movie",
        "media_id":       "tmdb_1",
        "frame":          50,
        "confidence":     0.75,
        "png":            _object_png_name(index),
    }
    json_path.write_text(json.dumps(meta), encoding="utf-8")
    return json_path


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

class TestSafeLabel(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(_safe_label("Horse"), "horse")

    def test_spaces_to_underscores(self):
        self.assertEqual(_safe_label("wild horse"), "wild_horse")

    def test_special_chars_replaced(self):
        self.assertEqual(_safe_label("horse!"), "horse_")

    def test_already_safe(self):
        self.assertEqual(_safe_label("cowboy"), "cowboy")


class TestCatalogDirHelpers(unittest.TestCase):
    def test_catalog_base_dir(self):
        p = catalog_base_dir("/project", "movie")
        self.assertEqual(p, Path("/project/data/silhouettes/catalog/movies"))

    def test_catalog_item_dir(self):
        p = catalog_item_dir("/project", "movie", "django_1966", "horse")
        self.assertEqual(
            p,
            Path("/project/data/silhouettes/catalog/movies/django_1966/horse"),
        )

    def test_catalog_item_dir_label_sanitized(self):
        p = catalog_item_dir("/project", "movie", "film", "Wild Horse")
        self.assertEqual(p.name, "wild_horse")


class TestObjectFilenames(unittest.TestCase):
    def test_png_name(self):
        self.assertEqual(_object_png_name(1),    "object_0001.png")
        self.assertEqual(_object_png_name(99),   "object_0099.png")
        self.assertEqual(_object_png_name(1000), "object_1000.png")

    def test_json_name(self):
        self.assertEqual(_object_json_name(1),  "object_0001.json")
        self.assertEqual(_object_json_name(42), "object_0042.json")


class TestNextObjectIndex(unittest.TestCase):
    def test_empty_dir_returns_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "horse"
            d.mkdir()
            self.assertEqual(_next_object_index(d), 1)

    def test_nonexistent_dir_returns_one(self):
        self.assertEqual(_next_object_index(Path("/does/not/exist")), 1)

    def test_existing_pngs(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "horse"
            d.mkdir()
            for i in (1, 2, 3):
                (d / _object_png_name(i)).touch()
            self.assertEqual(_next_object_index(d), 4)

    def test_gaps_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "horse"
            d.mkdir()
            (d / "object_0005.png").touch()
            self.assertEqual(_next_object_index(d), 6)


# ---------------------------------------------------------------------------
# Quality filters
# ---------------------------------------------------------------------------

class TestPassesQualityFilters(unittest.TestCase):
    def test_normal_mask_passes(self):
        m = {"area": 1000, "bbox": [10, 10, 50, 50]}
        frame_area = 100 * 100
        ok, reason = _passes_quality_filters(m, frame_area)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_too_small(self):
        m = {"area": 1, "bbox": [0, 0, 1, 1]}
        ok, reason = _passes_quality_filters(m, 100 * 100)
        self.assertFalse(ok)
        self.assertIn("small", reason)

    def test_too_large(self):
        m = {"area": 9000, "bbox": [0, 0, 100, 90]}
        ok, reason = _passes_quality_filters(m, 100 * 100)
        self.assertFalse(ok)
        self.assertIn("large", reason)

    def test_extreme_aspect_ratio(self):
        m = {"area": 500, "bbox": [0, 0, 200, 10]}
        ok, reason = _passes_quality_filters(m, 200 * 100)
        self.assertFalse(ok)
        self.assertIn("aspect", reason)


class TestTouchesBorder(unittest.TestCase):
    def _mask(self, seg: np.ndarray) -> dict:
        return {"segmentation": seg}

    def test_contained_mask_does_not_touch(self):
        seg = np.zeros((100, 100), dtype=bool)
        seg[10:20, 10:20] = True
        self.assertFalse(_touches_border(self._mask(seg), 100, 100))

    def test_top_border(self):
        seg = np.zeros((100, 100), dtype=bool)
        seg[0, 50] = True
        self.assertTrue(_touches_border(self._mask(seg), 100, 100))

    def test_bottom_border(self):
        seg = np.zeros((100, 100), dtype=bool)
        seg[99, 50] = True
        self.assertTrue(_touches_border(self._mask(seg), 100, 100))

    def test_left_border(self):
        seg = np.zeros((100, 100), dtype=bool)
        seg[50, 0] = True
        self.assertTrue(_touches_border(self._mask(seg), 100, 100))

    def test_right_border(self):
        seg = np.zeros((100, 100), dtype=bool)
        seg[50, 99] = True
        self.assertTrue(_touches_border(self._mask(seg), 100, 100))

    def test_none_segmentation_returns_true(self):
        self.assertTrue(_touches_border({"segmentation": None}, 100, 100))


# ---------------------------------------------------------------------------
# IoU and deduplication
# ---------------------------------------------------------------------------

class TestMasksIou(unittest.TestCase):
    def test_identical_masks(self):
        seg = np.zeros((10, 10), dtype=bool)
        seg[2:5, 2:5] = True
        self.assertAlmostEqual(_masks_iou(seg, seg), 1.0)

    def test_no_overlap(self):
        a = np.zeros((10, 10), dtype=bool)
        b = np.zeros((10, 10), dtype=bool)
        a[0:3, 0:3] = True
        b[7:10, 7:10] = True
        self.assertAlmostEqual(_masks_iou(a, b), 0.0)

    def test_partial_overlap(self):
        a = np.zeros((10, 10), dtype=bool)
        b = np.zeros((10, 10), dtype=bool)
        a[0:4, 0:4] = True   # 16 pixels
        b[2:6, 2:6] = True   # 16 pixels, overlap 2×2=4
        iou = _masks_iou(a, b)
        # intersection = 4, union = 16 + 16 - 4 = 28
        self.assertAlmostEqual(iou, 4 / 28, places=4)

    def test_empty_masks(self):
        a = np.zeros((10, 10), dtype=bool)
        b = np.zeros((10, 10), dtype=bool)
        self.assertEqual(_masks_iou(a, b), 0.0)


class TestDeduplicateMasks(unittest.TestCase):
    def _pair(self, y0, y1, score=0.9):
        seg = np.zeros((100, 100), dtype=bool)
        seg[y0:y1, 10:40] = True
        return ({"segmentation": seg, "area": int(seg.sum()), "bbox": [10, y0, 30, y1 - y0]}, score)

    def test_identical_masks_deduped(self):
        pair = self._pair(10, 40)
        result = _deduplicate_masks([pair, pair])
        self.assertEqual(len(result), 1)

    def test_non_overlapping_masks_both_kept(self):
        a = self._pair(10, 30, score=0.9)
        b = self._pair(60, 80, score=0.8)
        result = _deduplicate_masks([a, b])
        self.assertEqual(len(result), 2)

    def test_order_preserved_highest_score_first(self):
        high = self._pair(10, 40, score=0.9)
        low  = self._pair(10, 40, score=0.5)  # same region, lower score
        # Input must be sorted by score desc (caller contract)
        result = _deduplicate_masks([high, low])
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0][1], 0.9)


# ---------------------------------------------------------------------------
# PNG extraction
# ---------------------------------------------------------------------------

class TestExtractObjectPng(unittest.TestCase):
    def _rgb_image(self, w=100, h=100):
        arr = np.random.randint(0, 256, (h, w, 3), dtype=np.uint8)
        return Image.fromarray(arr, mode="RGB")

    def _mask_dict(self, h=100, w=100, y1=20, y2=40, x1=20, x2=50):
        seg = np.zeros((h, w), dtype=bool)
        seg[y1:y2, x1:x2] = True
        return {"segmentation": seg, "bbox": [x1, y1, x2 - x1, y2 - y1], "area": int(seg.sum())}

    def test_returns_rgba(self):
        img = self._rgb_image()
        mask = self._mask_dict()
        result = extract_object_png(img, mask)
        self.assertIsNotNone(result)
        self.assertEqual(result.mode, "RGBA")

    def test_output_size_approximately_mask_bbox(self):
        img = self._rgb_image()
        mask = self._mask_dict(y1=20, y2=40, x1=20, x2=50)
        result = extract_object_png(img, mask, pad_px=0)
        # Should be (x2-x1) × (y2-y1)
        self.assertEqual(result.size, (30, 20))

    def test_transparency_outside_mask(self):
        img = self._rgb_image()
        mask = self._mask_dict(y1=20, y2=40, x1=20, x2=50)
        result = extract_object_png(img, mask, pad_px=0)
        arr = np.array(result)
        # All pixels inside mask should have alpha = 255
        self.assertTrue((arr[:, :, 3] == 255).all())

    def test_empty_mask_returns_none(self):
        img = self._rgb_image()
        seg = np.zeros((100, 100), dtype=bool)
        mask = {"segmentation": seg, "bbox": [0, 0, 0, 0], "area": 0}
        result = extract_object_png(img, mask)
        self.assertIsNone(result)

    def test_padding_increases_output_size(self):
        img = self._rgb_image()
        mask = self._mask_dict(y1=30, y2=50, x1=30, x2=60)
        r_nopad = extract_object_png(img, mask, pad_px=0)
        r_pad   = extract_object_png(img, mask, pad_px=4)
        # padded version should be larger
        w0, h0 = r_nopad.size
        w1, h1 = r_pad.size
        self.assertGreater(w1, w0)
        self.assertGreater(h1, h0)


# ---------------------------------------------------------------------------
# scan_catalog
# ---------------------------------------------------------------------------

class TestScanCatalog(unittest.TestCase):
    def test_empty_base_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = scan_catalog(tmp, media_type="movie")
            self.assertEqual(records, [])

    def test_finds_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "silhouettes" / "catalog" / "movie"
            label_dir = base / "django_1966" / "horse"
            _make_catalog_entry(label_dir, 1)
            _make_catalog_entry(label_dir, 2)

            records = scan_catalog(tmp, media_type="movie")
            self.assertEqual(len(records), 2)

    def test_filter_by_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "silhouettes" / "catalog" / "movie"
            _make_catalog_entry(base / "film" / "horse",  1)
            _make_catalog_entry(base / "film" / "cowboy", 1)

            records = scan_catalog(tmp, media_type="movie", label="horse")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["label"], "horse")

    def test_filter_by_filename_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "silhouettes" / "catalog" / "movie"
            _make_catalog_entry(base / "film_a" / "horse", 1)
            _make_catalog_entry(base / "film_b" / "horse", 1)

            records = scan_catalog(tmp, media_type="movie", filename_stem="film_a")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["filename_stem"], "film_a")


# ---------------------------------------------------------------------------
# audit_catalog
# ---------------------------------------------------------------------------

class TestAuditCatalog(unittest.TestCase):
    def test_empty_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = audit_catalog(tmp, media_type="movie")
            self.assertEqual(report["total_objects"], 0)
            self.assertEqual(report["labels"], {})
            self.assertEqual(report["media_items"], {})

    def test_populated_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "silhouettes" / "catalog" / "movie"
            _make_catalog_entry(base / "film_a" / "horse",  1)
            _make_catalog_entry(base / "film_a" / "horse",  2)
            _make_catalog_entry(base / "film_a" / "cowboy", 1)
            _make_catalog_entry(base / "film_b" / "horse",  1)

            report = audit_catalog(tmp, media_type="movie")
            self.assertEqual(report["total_objects"], 4)
            self.assertEqual(report["labels"]["horse"],  3)
            self.assertEqual(report["labels"]["cowboy"], 1)
            self.assertEqual(report["media_items"]["film_a"], 3)
            self.assertEqual(report["media_items"]["film_b"], 1)

    def test_by_label_sorted_descending(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "silhouettes" / "catalog" / "movie"
            for i in range(1, 4):
                _make_catalog_entry(base / "film" / "horse", i)
            _make_catalog_entry(base / "film" / "cowboy", 1)

            report = audit_catalog(tmp, media_type="movie")
            by_label = report["by_label"]
            self.assertEqual(by_label[0][0], "horse")
            self.assertEqual(by_label[0][1], 3)

    def test_filter_by_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "silhouettes" / "catalog" / "movie"
            _make_catalog_entry(base / "film" / "horse",  1)
            _make_catalog_entry(base / "film" / "cowboy", 1)

            report = audit_catalog(tmp, media_type="movie", label="horse")
            self.assertEqual(report["total_objects"], 1)
            self.assertNotIn("cowboy", report["labels"])


# ---------------------------------------------------------------------------
# clear_catalog
# ---------------------------------------------------------------------------

class TestClearCatalog(unittest.TestCase):
    def _populate(self, base: Path):
        _make_catalog_entry(base / "film_a" / "horse",  1)
        _make_catalog_entry(base / "film_a" / "horse",  2)
        _make_catalog_entry(base / "film_a" / "cowboy", 1)
        _make_catalog_entry(base / "film_b" / "horse",  1)

    def test_dry_run_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "silhouettes" / "catalog" / "movie"
            self._populate(base)

            result = clear_catalog(tmp, media_type="movie", dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertGreater(result["deleted_files"], 0)
            # Files must still exist
            self.assertTrue((base / "film_a" / "horse" / "object_0001.png").exists())

    def test_clear_by_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "silhouettes" / "catalog" / "movie"
            self._populate(base)

            clear_catalog(tmp, media_type="movie", label="horse", dry_run=False)

            # horse files gone
            self.assertFalse((base / "film_a" / "horse" / "object_0001.png").exists())
            # cowboy remains
            self.assertTrue((base / "film_a" / "cowboy" / "object_0001.png").exists())

    def test_clear_by_filename_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "silhouettes" / "catalog" / "movie"
            self._populate(base)

            clear_catalog(tmp, media_type="movie", filename_stem="film_a", dry_run=False)

            # film_a gone
            self.assertFalse((base / "film_a").exists())
            # film_b still present
            self.assertTrue((base / "film_b" / "horse" / "object_0001.png").exists())

    def test_clear_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data" / "silhouettes" / "catalog" / "movie"
            self._populate(base)

            result = clear_catalog(tmp, media_type="movie", dry_run=False)
            self.assertFalse(base.exists())
            self.assertGreater(result["deleted_files"], 0)

    def test_clear_nonexistent_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = clear_catalog(tmp, media_type="movie", dry_run=False)
            self.assertEqual(result["deleted_files"], 0)


if __name__ == "__main__":
    unittest.main()
