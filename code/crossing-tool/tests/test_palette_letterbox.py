from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import data.palette as palette_mod
from data.palette import (
    PaletteAnalysisError,
    _detect_content_bbox,
    _extract_fg_bg_full,
    create_palette_for_movie,
    create_thumbnail_palette,
    load_palette,
    load_thumbnail_palette,
)


class CenterSegmenter:
    model_name = "sam3-test"

    def __init__(self):
        self.images = []

    def segment_palette(self, image):
        pixels = np.asarray(image)
        self.images.append(pixels.copy())
        height, width = pixels.shape[:2]
        top, bottom = height // 4, height - height // 4
        left, right = width // 4, width - width // 4
        mask = np.zeros((height, width), dtype=bool)
        mask[top:bottom, left:right] = True
        return [{
            "segmentation": mask,
            "bbox": [left, top, right - left, bottom - top],
        }]


def _barred_image(
    width=160,
    height=120,
    *,
    top=0,
    bottom=0,
    left=0,
    right=0,
    bar_color=(4, 4, 4),
    content_color=(120, 80, 40),
):
    pixels = np.full((height, width, 3), content_color, dtype=np.uint8)
    if top:
        pixels[:top] = bar_color
    if bottom:
        pixels[height - bottom:] = bar_color
    if left:
        pixels[:, :left] = bar_color
    if right:
        pixels[:, width - right:] = bar_color
    return Image.fromarray(pixels, "RGB")


def _write_barred_image(path: Path, **kwargs):
    path.parent.mkdir(parents=True, exist_ok=True)
    _barred_image(**kwargs).save(path, format="PNG")
    return path


def test_no_bars_returns_full_image_bbox():
    result = _detect_content_bbox(_barred_image())

    assert result == {
        "bbox": [0, 0, 160, 120],
        "top": 0,
        "bottom": 0,
        "left": 0,
        "right": 0,
        "detected": False,
        "detected_horizontal": False,
        "detected_vertical": False,
    }


def test_horizontal_letterbox_excludes_top_and_bottom_bars():
    result = _detect_content_bbox(_barred_image(top=12, bottom=14))

    assert result["bbox"] == [0, 12, 160, 106]
    assert result["detected"] is True
    assert result["detected_horizontal"] is True
    assert result["detected_vertical"] is False
    assert (result["top"], result["bottom"]) == (12, 14)


def test_top_bar_is_detected_without_bottom_bar():
    result = _detect_content_bbox(_barred_image(top=12))

    assert result["bbox"] == [0, 12, 160, 120]
    assert (result["top"], result["bottom"]) == (12, 0)
    assert result["detected_horizontal"] is True


def test_bottom_bar_is_detected_without_top_bar():
    result = _detect_content_bbox(_barred_image(bottom=14))

    assert result["bbox"] == [0, 0, 160, 106]
    assert (result["top"], result["bottom"]) == (0, 14)
    assert result["detected_horizontal"] is True


def test_pillarbox_excludes_left_and_right_bars():
    result = _detect_content_bbox(_barred_image(left=10, right=16))

    assert result["bbox"] == [10, 0, 144, 120]
    assert result["detected"] is True
    assert result["detected_horizontal"] is False
    assert result["detected_vertical"] is True
    assert (result["left"], result["right"]) == (10, 16)


def test_left_bar_is_detected_without_right_bar():
    result = _detect_content_bbox(_barred_image(left=10))

    assert result["bbox"] == [10, 0, 160, 120]
    assert (result["left"], result["right"]) == (10, 0)
    assert result["detected_vertical"] is True


def test_right_bar_is_detected_without_left_bar():
    result = _detect_content_bbox(_barred_image(right=16))

    assert result["bbox"] == [0, 0, 144, 120]
    assert (result["left"], result["right"]) == (0, 16)
    assert result["detected_vertical"] is True


def test_combined_letterbox_and_pillarbox():
    result = _detect_content_bbox(
        _barred_image(top=12, bottom=14, left=10, right=16)
    )

    assert result["bbox"] == [10, 12, 144, 106]
    assert result["detected_horizontal"] is True
    assert result["detected_vertical"] is True


def test_dark_scene_without_continuous_near_black_bars_is_not_cropped():
    image = Image.new("RGB", (160, 120), (20, 20, 20))

    result = _detect_content_bbox(image)

    assert result["bbox"] == [0, 0, 160, 120]
    assert result["detected"] is False


def test_dark_chromatic_scene_border_is_not_a_black_bar():
    image = _barred_image(
        top=20,
        bottom=20,
        left=20,
        right=20,
        bar_color=(10, 10, 60),
    )

    result = _detect_content_bbox(image)

    assert result["bbox"] == [0, 0, 160, 120]
    assert result["detected"] is False


def test_internal_black_band_is_not_edge_connected_and_is_not_cropped():
    pixels = np.full((120, 160, 3), (120, 80, 40), dtype=np.uint8)
    pixels[40:60] = 0

    result = _detect_content_bbox(Image.fromarray(pixels, "RGB"))

    assert result["bbox"] == [0, 0, 160, 120]
    assert result["detected"] is False


def test_thin_bars_below_minimum_thickness_are_not_detected():
    image = _barred_image(width=200, height=200, top=2, bottom=2)

    result = _detect_content_bbox(image)

    assert result["bbox"] == [0, 0, 200, 200]
    assert result["detected"] is False


def test_bars_at_minimum_thickness_are_detected():
    image = _barred_image(width=200, height=200, top=3, bottom=3)

    result = _detect_content_bbox(image)

    assert result["bbox"] == [0, 3, 200, 197]
    assert result["detected_horizontal"] is True


@pytest.mark.parametrize(
    ("bar_color", "detected"),
    [((16, 16, 16), True), ((17, 17, 17), False)],
)
def test_near_black_luminance_boundary(bar_color, detected):
    image = _barred_image(top=3, bottom=3, bar_color=bar_color)

    result = _detect_content_bbox(image)

    assert result["detected_horizontal"] is detected


@pytest.mark.parametrize(
    ("bar_color", "detected"),
    [((8, 0, 0), True), ((9, 0, 0), False)],
)
def test_near_black_channel_spread_boundary(bar_color, detected):
    image = _barred_image(top=3, bottom=3, bar_color=bar_color)

    result = _detect_content_bbox(image)

    assert result["detected_horizontal"] is detected


@pytest.mark.parametrize(
    ("content_luma", "detected"),
    [(19, False), (20, True)],
)
def test_edge_to_interior_luminance_discontinuity_boundary(
    content_luma, detected,
):
    image = _barred_image(
        top=3,
        bar_color=(0, 0, 0),
        content_color=(content_luma, content_luma, content_luma),
    )

    result = _detect_content_bbox(image)

    assert result["detected_horizontal"] is detected
    expected = [0, 3, 160, 120] if detected else [0, 0, 160, 120]
    assert result["bbox"] == expected


@pytest.mark.parametrize(
    ("dark_pixels", "detected"),
    [(97, False), (98, True)],
)
def test_row_dark_coverage_boundary(dark_pixels, detected):
    pixels = np.full((100, 100, 3), (100, 100, 100), dtype=np.uint8)
    pixels[:3, :dark_pixels] = 0
    pixels[-3:, :dark_pixels] = 0
    image = Image.fromarray(pixels, "RGB")

    result = _detect_content_bbox(image)

    assert result["detected_horizontal"] is detected
    expected = [0, 3, 100, 97] if detected else [0, 0, 100, 100]
    assert result["bbox"] == expected


def test_invalid_detection_input_is_explicit_failure():
    with pytest.raises(PaletteAnalysisError, match="could not analyze"):
        _detect_content_bbox(None)


def test_large_image_detection_uses_bounded_axis_projections(monkeypatch):
    image = _barred_image(width=1024, height=768, top=20, bottom=20)
    resize_calls = []
    original_resize = Image.Image.resize

    def recording_resize(self, size, *args, **kwargs):
        resize_calls.append(size)
        return original_resize(self, size, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", recording_resize)

    result = _detect_content_bbox(image)

    assert resize_calls == [(512, 768), (1024, 512)]
    assert result["bbox"] == [0, 20, 1024, 748]


def test_palette_segmentation_receives_cropped_active_image(tmp_path):
    path = _write_barred_image(
        tmp_path / "letterboxed.png",
        top=20,
        bottom=20,
        content_color=(180, 40, 20),
    )
    source = np.asarray(Image.open(path).convert("RGB")).copy()
    source[45:75, 60:100] = (20, 40, 180)
    Image.fromarray(source, "RGB").save(path)
    segmenter = CenterSegmenter()

    foreground, background, diagnostics = _extract_fg_bg_full(
        path, segmenter=segmenter
    )

    segmented_image = segmenter.images[0]
    assert segmented_image.shape == (256, 256, 3)
    assert np.all(segmented_image[0, 0] == (180, 40, 20))
    assert background["rgb"][0] > background["rgb"][2]
    assert background["rgb"] != [0, 0, 0]
    assert foreground["palette"]
    assert diagnostics["source_image"] == {
        "width": 160,
        "height": 120,
        "content_bbox": {
            "left": 0,
            "top": 20,
            "right": 160,
            "bottom": 100,
        },
        "letterbox": {
            "detected": True,
            "top": 20,
            "bottom": 20,
            "left": 0,
            "right": 0,
            "detected_horizontal": True,
            "detected_vertical": False,
        },
    }


def test_no_bars_diagnostics_record_full_source_bbox(tmp_path):
    path = _write_barred_image(tmp_path / "full-frame.png")

    _foreground, _background, diagnostics = _extract_fg_bg_full(
        path, segmenter=CenterSegmenter()
    )

    source = diagnostics["source_image"]
    assert source["content_bbox"] == {
        "left": 0,
        "top": 0,
        "right": 160,
        "bottom": 120,
    }
    assert source["letterbox"]["detected"] is False
    assert source["letterbox"]["top"] == 0
    assert source["letterbox"]["bottom"] == 0
    assert source["letterbox"]["left"] == 0
    assert source["letterbox"]["right"] == 0


def test_best_frame_and_thumbnail_use_same_detector(tmp_path, monkeypatch):
    metadata = {
        "filename": "The Searchers (1956).mp4",
        "media_type": "movie",
        "media_id": "tmdb_3114",
        "title": "The Searchers",
        "year": 1956,
        "tmdb": "3114",
    }
    thumbnail = (
        tmp_path / "media" / "thumbnails" / "movie"
        / "The Searchers (1956).jpg"
    )
    _write_barred_image(thumbnail, top=12, bottom=12)

    from services.frame_match import best_frame_path

    shot_id = "tmdb_3114@f000000-f000100"
    frame = best_frame_path(
        str(tmp_path), "movie", metadata["filename"], shot_id
    )
    _write_barred_image(frame, top=12, bottom=12)
    entry = {
        "movie": {"filename": metadata["filename"]},
        "shot": {
            "shot_id": shot_id,
            "best_frame": {"frame": 50, "score": 0.9, "method": "clip"},
        },
    }

    detector_calls = []
    real_detector = palette_mod._detect_content_bbox

    def recording_detector(image):
        detector_calls.append(image.size)
        return real_detector(image)

    foreground = {
        "rgb": [20, 40, 180], "lab": [30.0, 10.0, -30.0],
        "luminance": 0.3, "chroma": 0.4, "palette": [], "coverage": 0.25,
    }
    background = {
        "rgb": [120, 80, 40], "lab": [45.0, 10.0, 20.0],
        "luminance": 0.45, "chroma": 0.3, "palette": [], "coverage": 0.75,
    }

    def fake_figure(_arr, *, segmenter):
        assert segmenter is not None
        return foreground, background, {"method_used": "figure"}

    monkeypatch.setattr(palette_mod, "_detect_content_bbox", recording_detector)
    monkeypatch.setattr(palette_mod, "_extract_fg_bg_figure", fake_figure)
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_args: object()
    )
    monkeypatch.setattr(palette_mod, "get_metadata", lambda *_a, **_k: [metadata])
    monkeypatch.setattr(palette_mod, "load_annotation_items", lambda *_a: [entry])
    monkeypatch.setattr(
        palette_mod,
        "read_shotlist",
        lambda *_a: (_ for _ in ()).throw(FileNotFoundError()),
    )

    create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "movie", metadata=metadata
    )
    create_palette_for_movie(
        str(tmp_path), metadata["filename"], "movie", force=True
    )

    thumbnail_cache = load_thumbnail_palette(
        str(tmp_path), metadata["media_id"], "movie"
    )
    best_frame_cache = load_palette(
        str(tmp_path), metadata["filename"], "movie"
    )
    thumbnail_source = thumbnail_cache["thumbnail"]["diagnostics"]["source_image"]
    best_frame_source = best_frame_cache["shots"][0]["diagnostics"]["source_image"]

    assert detector_calls == [(160, 120), (160, 120)]
    expected_bbox = {"left": 0, "top": 12, "right": 160, "bottom": 108}
    assert thumbnail_source["content_bbox"] == expected_bbox
    assert best_frame_source["content_bbox"] == expected_bbox
    assert thumbnail_source["letterbox"]["detected"] is True
    assert best_frame_source["letterbox"]["detected"] is True
