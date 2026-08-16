import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

import data.palette as palette_mod
from data.palette import (
    PaletteAnalysisError,
    _agglomerative_palette,
    _extract_fg_bg_figure,
    _extract_fg_bg_full,
    _extract_seeds_superpixels,
)


class CenterSegmenter:
    model_name = "sam3-test"

    def segment_concept(self, image, concept):
        assert concept == "subject"
        width, height = image.size
        mask = np.zeros((height, width), dtype=bool)
        mask[height // 4:3 * height // 4, width // 4:3 * width // 4] = True
        return [{
            "segmentation": mask,
            "bbox": [width // 4, height // 4, width // 2, height // 2],
        }]


SEMANTIC_ANNOTATION = {
    "humans": ["subject"],
    "animals": [],
    "objects": [],
    "setting": "",
}


def _install_fake_seeds(monkeypatch, labels, calls=None):
    calls = calls if calls is not None else {}

    class FakeSeeds:
        def iterate(self, image, iterations):
            calls["iterate_image"] = image.copy()
            calls["iterations"] = iterations

        def getLabels(self):
            return labels.copy()

    def create_seeds(*args):
        calls["factory_args"] = args
        return FakeSeeds()

    fake_cv2 = SimpleNamespace(
        ximgproc=SimpleNamespace(createSuperpixelSEEDS=create_seeds)
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    return calls


def _quadrant_labels(size=8):
    labels = np.zeros((size, size), dtype=np.int32)
    half = size // 2
    labels[:half, half:] = 1
    labels[half:, :half] = 2
    labels[half:, half:] = 3
    return labels


def _full_region_masks(height, width):
    fg_mask = np.zeros((height, width), dtype=bool)
    fg_mask[:height // 2] = True
    return fg_mask, ~fg_mask


def _candidate(rgb, size):
    lab = palette_mod._rgb_to_lab(np.asarray([rgb], dtype=np.uint8))[0]
    return {
        "rgb": tuple(rgb),
        "lab": [float(value) for value in lab],
        "luminance": float(lab[0]) / 100.0,
        "chroma": float(np.sqrt(lab[1] ** 2 + lab[2] ** 2)) / 100.0,
        "weight": float(size),
        "size": size,
    }


def test_seeds_is_invoked_and_reduces_textured_regions_to_spatial_means(monkeypatch):
    labels = _quadrant_labels()
    calls = _install_fake_seeds(monkeypatch, labels)
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    base_colors = (
        (20, 40, 60),
        (80, 100, 120),
        (140, 160, 180),
        (200, 210, 220),
    )
    for label_id, base in enumerate(base_colors):
        mask = labels == label_id
        texture = np.indices(mask.shape).sum(axis=0)[mask] % 2
        image[mask] = np.asarray(base, dtype=np.uint8) + texture[:, None] * 2
    fg_mask, bg_mask = _full_region_masks(8, 8)

    foreground, background, diagnostics = _extract_seeds_superpixels(
        image, fg_mask, bg_mask
    )

    assert calls["factory_args"] == (8, 8, 3, 1024, 4, 2, 5, False)
    assert calls["iterations"] == palette_mod._SEEDS_ITERATIONS
    np.testing.assert_array_equal(calls["iterate_image"], image[:, :, ::-1])
    assert len(foreground) == len(background) == 2
    assert len(foreground) + len(background) == 4
    assert foreground[0]["rgb"] == [21, 41, 61]
    assert foreground[1]["rgb"] == [81, 101, 121]
    assert all(item["pixel_count"] == 16 for item in foreground + background)
    assert diagnostics["superpixel_count"] == 4


def test_boundary_superpixels_use_majority_region_without_colour_contamination(
    monkeypatch,
):
    labels = np.zeros((4, 4), dtype=np.int32)
    labels[:, 2:] = 1
    _install_fake_seeds(monkeypatch, labels)
    fg_mask = np.zeros((4, 4), dtype=bool)
    fg_positions = [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0)]
    tie_positions = [(0, 2), (0, 3), (1, 2), (1, 3)]
    for position in fg_positions + tie_positions:
        fg_mask[position] = True
    bg_mask = ~fg_mask

    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[labels == 0] = (250, 0, 0)
    image[labels == 1] = (200, 0, 0)
    image[(labels == 0) & fg_mask] = (10, 20, 30)
    image[(labels == 1) & bg_mask] = (40, 50, 60)

    foreground, background, diagnostics = _extract_seeds_superpixels(
        image, fg_mask, bg_mask
    )

    assert foreground == [{
        "label": 0,
        "rgb": [10, 20, 30],
        "pixel_count": 8,
        "region_pixel_count": 5,
    }]
    assert background == [{
        "label": 1,
        "rgb": [40, 50, 60],
        "pixel_count": 8,
        "region_pixel_count": 4,
    }]
    assert diagnostics["boundary_crossing_superpixel_count"] == 2
    assert diagnostics["superpixel_region_rule"] == (
        "strict_foreground_majority_background_tie"
    )


def test_agglomerative_palette_preserves_superpixel_area_weight():
    representatives = [
        {"rgb": [220, 20, 20], "pixel_count": 2000},
        {"rgb": [220, 20, 20], "pixel_count": 50},
        {"rgb": [20, 20, 220], "pixel_count": 25},
        {"rgb": [20, 20, 220], "pixel_count": 25},
    ]

    candidates = _agglomerative_palette(
        representatives, n_clusters=2, n_palette=2
    )

    assert candidates[0]["size"] == 2050
    assert candidates[0]["rgb"] == (220, 20, 20)
    assert sorted(candidate["size"] for candidate in candidates) == [50, 2050]


def test_ward_receives_superpixel_means_not_raw_pixels(monkeypatch):
    import scipy.cluster.hierarchy as hierarchy

    real_linkage = hierarchy.linkage
    captured = []

    def recording_linkage(values, *args, **kwargs):
        captured.append(values.copy())
        return real_linkage(values, *args, **kwargs)

    monkeypatch.setattr(hierarchy, "linkage", recording_linkage)
    representatives = [
        {"rgb": [10, 20, 30], "pixel_count": 100},
        {"rgb": [40, 50, 60], "pixel_count": 200},
        {"rgb": [70, 80, 90], "pixel_count": 300},
        {"rgb": [100, 110, 120], "pixel_count": 400},
    ]

    _agglomerative_palette(representatives, n_clusters=2, n_palette=2)

    assert len(captured) == 1
    assert captured[0].shape == (4, 3)
    expected = palette_mod._rgb_to_lab(
        np.asarray([item["rgb"] for item in representatives], dtype=np.uint8)
    )
    np.testing.assert_allclose(captured[0], expected)


def test_sam_masks_are_computed_before_independent_region_clustering(monkeypatch):
    image = np.full((16, 16, 3), 80, dtype=np.uint8)
    foreground_reps = [
        {"rgb": [180, 40, 20], "pixel_count": 80},
        {"rgb": [170, 50, 30], "pixel_count": 40},
    ]
    background_reps = [
        {"rgb": [20, 40, 120], "pixel_count": 100},
        {"rgb": [30, 50, 130], "pixel_count": 36},
    ]
    order = []
    clustered = []

    def fake_masks(arr, *, annotation, segmenter):
        assert annotation == SEMANTIC_ANNOTATION
        order.append("sam")
        fg_mask, bg_mask = _full_region_masks(*arr.shape[:2])
        return fg_mask, bg_mask, {
            "foreground_mode": "semantic",
            "semantic_categories_used": ["humans"],
            "semantic_concepts_used": ["subject"],
            "semantic_mask_count": 1,
            "semantic_foreground_coverage": 0.5,
            "semantic_category_pixel_counts": {"humans": int(fg_mask.sum())},
        }

    def fake_superpixels(arr, fg_mask, bg_mask):
        assert order == ["sam"]
        order.append("seeds")
        return foreground_reps, background_reps, {
            "superpixels_used": True,
            "superpixel_count": 4,
        }

    def fake_cluster(representatives, *_args):
        clustered.append(representatives)
        total = sum(item["pixel_count"] for item in representatives)
        return [_candidate(representatives[0]["rgb"], total)]

    monkeypatch.setattr(palette_mod, "_select_semantic_foreground", fake_masks)
    monkeypatch.setattr(palette_mod, "_extract_seeds_superpixels", fake_superpixels)
    monkeypatch.setattr(palette_mod, "_agglomerative_palette", fake_cluster)

    _extract_fg_bg_figure(
        image,
        annotation=SEMANTIC_ANNOTATION,
        segmenter=object(),
    )

    assert order == ["sam", "seeds"]
    assert clustered == [foreground_reps, background_reps]
    assert clustered[0] is not clustered[1]


def test_missing_seeds_capability_is_explicit_and_has_no_fallback(
    monkeypatch,
):
    fake_cv2 = SimpleNamespace(ximgproc=SimpleNamespace())
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    cluster_calls = []
    monkeypatch.setattr(
        palette_mod,
        "_agglomerative_palette",
        lambda *_args, **_kwargs: cluster_calls.append(True),
    )

    with pytest.raises(
        PaletteAnalysisError,
        match=r"cv2\.ximgproc\.createSuperpixelSEEDS is missing",
    ):
        _extract_fg_bg_figure(
            np.full((32, 32, 3), 100, dtype=np.uint8),
            annotation=SEMANTIC_ANNOTATION,
            segmenter=CenterSegmenter(),
        )

    assert cluster_calls == []


def test_mean_shift_is_absent_from_executable_palette_path():
    assert not hasattr(palette_mod, "_mean_shift_simplify")
    assert "pyrMeanShiftFiltering" not in inspect.getsource(palette_mod)


def test_real_seeds_is_deterministic_and_bounded_at_256():
    rows, columns = np.indices((256, 256))
    image = np.stack([
        (rows * 3 + columns) % 256,
        (rows + columns * 5) % 256,
        (rows * 7 + columns * 2) % 256,
    ], axis=2).astype(np.uint8)
    fg_mask = np.zeros((256, 256), dtype=bool)
    fg_mask[48:208, 48:208] = True
    bg_mask = ~fg_mask

    first = _extract_seeds_superpixels(image, fg_mask, bg_mask)
    second = _extract_seeds_superpixels(image, fg_mask, bg_mask)

    assert first == second
    foreground, background, diagnostics = first
    assert 0 < diagnostics["superpixel_count"] <= 1024
    assert len(foreground) + len(background) == diagnostics["superpixel_count"]
    assert diagnostics["max_superpixel_area"] <= 256 * 256


def test_crop_and_resize_happen_before_seeds(monkeypatch, tmp_path):
    source = np.zeros((80, 100, 3), dtype=np.uint8)
    source[10:] = (120, 30, 20)
    image_path = tmp_path / "barred.png"
    Image.fromarray(source, "RGB").save(image_path)
    seen = []

    monkeypatch.setattr(
        palette_mod,
        "_detect_content_bbox",
        lambda _image: {
            "bbox": [0, 10, 100, 80],
            "top": 10,
            "bottom": 0,
            "left": 0,
            "right": 0,
            "detected": True,
            "detected_horizontal": True,
            "detected_vertical": False,
        },
    )
    monkeypatch.setattr(
        palette_mod,
        "_select_semantic_foreground",
        lambda arr, *, annotation, segmenter: (
            *_full_region_masks(*arr.shape[:2]),
            {
                "foreground_mode": "semantic",
                "semantic_categories_used": ["humans"],
                "semantic_concepts_used": ["subject"],
                "semantic_mask_count": 1,
                "semantic_foreground_coverage": 0.5,
                "semantic_category_pixel_counts": {"humans": arr.size // 6},
            },
        ),
    )

    def fake_superpixels(arr, _fg_mask, _bg_mask):
        seen.append(arr.copy())
        return (
            [
                {"rgb": [120, 30, 20], "pixel_count": 100},
                {"rgb": [121, 31, 21], "pixel_count": 100},
            ],
            [
                {"rgb": [120, 30, 20], "pixel_count": 100},
                {"rgb": [119, 29, 19], "pixel_count": 100},
            ],
            {"superpixels_used": True, "superpixel_count": 4},
        )

    monkeypatch.setattr(
        palette_mod, "_extract_seeds_superpixels", fake_superpixels
    )

    _extract_fg_bg_full(
        image_path,
        annotation=SEMANTIC_ANNOTATION,
        segmenter=object(),
    )

    assert len(seen) == 1
    assert seen[0].shape == (256, 256, 3)
    assert np.all(seen[0] == np.asarray([120, 30, 20], dtype=np.uint8))


def test_thumbnail_and_best_frame_share_revised_full_pipeline(monkeypatch, tmp_path):
    image_path = tmp_path / "source.png"
    Image.new("RGB", (32, 32), (90, 60, 30)).save(image_path)
    metadata = {
        "filename": "movie.mp4",
        "media_id": "movie_id",
        "media_type": "movie",
        "title": "Movie",
    }
    calls = []
    foreground = {
        "rgb": [10, 20, 30], "lab": [10.0, 0.0, 0.0],
        "luminance": 0.1, "chroma": 0.0, "palette": [], "coverage": 0.5,
    }
    background = {
        "rgb": [40, 50, 60], "lab": [20.0, 0.0, 0.0],
        "luminance": 0.2, "chroma": 0.0, "palette": [], "coverage": 0.5,
    }

    def fake_full(path, *, annotation, segmenter):
        calls.append((Path(path), annotation, segmenter))
        return foreground, background, {
            "method_used": "figure", "superpixels_used": True,
        }

    segmenter = object()
    monkeypatch.setattr(palette_mod, "_extract_fg_bg_full", fake_full)
    monkeypatch.setattr(
        palette_mod, "resolve_thumbnail_path", lambda *_args: image_path
    )
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_args: segmenter
    )
    monkeypatch.setattr(
        palette_mod,
        "_resolve_thumbnail_annotation",
        lambda *_args: SEMANTIC_ANNOTATION,
    )
    monkeypatch.setattr(
        "services.frame_match.best_frame_path", lambda *_args: image_path
    )

    palette_mod.create_thumbnail_palette(
        str(tmp_path), "movie.mp4", "movie", metadata=metadata
    )
    palette_mod._process_one_shot(
        str(tmp_path),
        "movie.mp4",
        "movie",
        {"shot": {
            "shot_id": "movie_id@f000000-f000010",
            "annotation": SEMANTIC_ANNOTATION,
            "best_frame": {"frame": 5},
        }},
        0,
        None,
        segmenter=segmenter,
    )

    assert calls == [
        (image_path, SEMANTIC_ANNOTATION, segmenter),
        (image_path, SEMANTIC_ANNOTATION, segmenter),
    ]
