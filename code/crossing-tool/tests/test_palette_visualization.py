import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

import cli as cli_mod
import data.palette as palette_mod
from data.palette import PaletteAnalysisError


FOREGROUND = {
    "rgb": [30, 60, 210],
    "lab": [35.0, 20.0, -60.0],
    "luminance": 0.35,
    "chroma": 0.63,
    "palette": [],
    "coverage": 0.25,
}
BACKGROUND = {
    "rgb": [190, 40, 20],
    "lab": [45.0, 55.0, 45.0],
    "luminance": 0.45,
    "chroma": 0.71,
    "palette": [],
    "coverage": 0.75,
}
SEMANTIC_ANNOTATION = {
    "humans": ["subject"],
    "animals": [],
    "objects": [],
    "setting": "",
}


def _diagnostics(width, height, content_bbox=None):
    bbox = content_bbox or {
        "left": 0,
        "top": 0,
        "right": width,
        "bottom": height,
    }
    return {
        "method_used": "semantic-figure",
        "foreground_mode": "semantic",
        "semantic_categories_used": ["humans"],
        "semantic_concepts_used": ["subject"],
        "semantic_mask_count": 1,
        "semantic_foreground_coverage": 0.25,
        "semantic_category_pixel_counts": {"humans": 16384},
        "source_image": {
            "width": width,
            "height": height,
            "content_bbox": bbox,
            "letterbox": {
                "detected": bbox != {
                    "left": 0, "top": 0, "right": width, "bottom": height,
                },
                "top": bbox["top"],
                "bottom": height - bbox["bottom"],
                "left": bbox["left"],
                "right": width - bbox["right"],
                "detected_horizontal": bbox["top"] > 0 or bbox["bottom"] < height,
                "detected_vertical": bbox["left"] > 0 or bbox["right"] < width,
            },
        },
    }


def _render(tmp_path, size, content_bbox=None):
    width, height = size
    source_path = tmp_path / "source.png"
    source_pixels = np.full((height, width, 3), (80, 100, 120), dtype=np.uint8)
    Image.fromarray(source_pixels, "RGB").save(source_path)
    semantic_mask = np.zeros((256, 256), dtype=bool)
    semantic_mask[64:192, 64:192] = True
    seeds_labels = np.zeros((256, 256), dtype=np.int32)
    seeds_labels[:128, 128:] = 1
    seeds_labels[128:, :128] = 2
    seeds_labels[128:, 128:] = 3
    diagnostics = _diagnostics(width, height, content_bbox)

    palette_mod._write_palette_diagnostic_success(
        str(tmp_path),
        "game_test_12345678",
        "source.mp4",
        "gameplay",
        FOREGROUND,
        BACKGROUND,
        diagnostics,
        {
            "source_image": Image.fromarray(source_pixels, "RGB"),
            "semantic_foreground_mask": semantic_mask,
            "seeds_labels": seeds_labels,
            "seeds_regions": [
                {"label": 0, "assignment": "background", "boundary_crossing": False, "tie": False},
                {"label": 1, "assignment": "background", "boundary_crossing": True, "tie": True},
                {"label": 2, "assignment": "foreground", "boundary_crossing": True, "tie": False},
                {"label": 3, "assignment": "foreground", "boundary_crossing": False, "tie": False},
            ],
            "ward": {
                "foreground": {
                    "superpixel_labels": [2, 3],
                    "cluster_labels": [1, 2],
                    "ranked_candidates": [
                        {"cluster_id": 1, "rgb": [30, 60, 210]},
                        {"cluster_id": 2, "rgb": [50, 80, 180]},
                    ],
                },
                "background": {
                    "superpixel_labels": [0, 1],
                    "cluster_labels": [1, 2],
                    "ranked_candidates": [
                        {"cluster_id": 1, "rgb": [190, 40, 20]},
                        {"cluster_id": 2, "rgb": [160, 70, 40]},
                    ],
                },
            },
        },
    )
    output_dir = tmp_path / "outputs" / "test" / "palette"
    return source_pixels, diagnostics, output_dir


@pytest.mark.parametrize(
    "size",
    [(400, 300), (320, 180), (200, 300)],
    ids=["four_by_three", "sixteen_by_nine", "portrait_four_by_six"],
)
def test_diagnostic_images_preserve_source_dimensions_and_aspect(tmp_path, size):
    source_pixels, _diagnostic, output_dir = _render(tmp_path, size)

    with Image.open(output_dir / "game_test_12345678_source.png") as source:
        assert source.size == size
        np.testing.assert_array_equal(np.asarray(source), source_pixels)
    with Image.open(output_dir / "game_test_12345678_palette.png") as palette:
        assert palette.size == size
    with Image.open(output_dir / "game_test_12345678_semantic.png") as semantic:
        assert semantic.size == size
    with Image.open(output_dir / "game_test_12345678_seeds.png") as seeds:
        assert seeds.size == size
    with Image.open(
        output_dir / "game_test_12345678_seeds_assignment.png"
    ) as assignment:
        assert assignment.size == size
    with Image.open(output_dir / "game_test_12345678_clusters.png") as clusters:
        assert clusters.size == size


def test_palette_image_uses_exact_colors_and_centered_circle(tmp_path):
    size = (400, 300)
    _source, _diagnostic, output_dir = _render(tmp_path, size)

    with Image.open(output_dir / "game_test_12345678_palette.png") as opened:
        pixels = np.asarray(opened.convert("RGB"))

    assert pixels[0, 0].tolist() == BACKGROUND["rgb"]
    assert pixels[size[1] // 2, size[0] // 2].tolist() == FOREGROUND["rgb"]
    foreground_pixels = np.all(pixels == np.asarray(FOREGROUND["rgb"]), axis=2)
    rows, columns = np.where(foreground_pixels)
    assert (columns.min() + columns.max()) / 2 == pytest.approx(size[0] / 2, abs=0.5)
    assert (rows.min() + rows.max()) / 2 == pytest.approx(size[1] / 2, abs=0.5)
    diameter = max(columns.max() - columns.min() + 1, rows.max() - rows.min() + 1)
    assert 0.25 <= diameter / min(size) <= 0.35


def test_diagnostic_json_records_letterbox_bbox_and_semantic_selection(tmp_path):
    bbox = {"left": 8, "top": 20, "right": 392, "bottom": 280}
    _source, diagnostics, output_dir = _render(
        tmp_path, (400, 300), content_bbox=bbox
    )

    document = json.loads(
        (output_dir / "game_test_12345678.json").read_text(encoding="utf-8")
    )
    assert document["status"] == "ok"
    assert document["source_image"] == {
        **diagnostics["source_image"],
        "stage": "original_before_letterbox_crop",
    }
    assert document["source_image"]["content_bbox"] == bbox
    assert document["semantic_selection"] == {
        "foreground_mode": "semantic",
        "categories_used": ["humans"],
        "concepts_used": ["subject"],
        "mask_count": 1,
        "foreground_coverage": 0.25,
        "category_pixel_counts": {"humans": 16384},
    }
    assert document["foreground"] == FOREGROUND
    assert document["background"] == BACKGROUND
    assert "source_image" not in document["diagnostics"]
    assert document["pipeline"]["seeds"] == {
        "count": 4,
        "foreground_count": 2,
        "background_count": 2,
        "boundary_crossing_count": 2,
        "tie_count": 1,
    }
    assert document["pipeline"]["ward_candidate_colors"] == {
        "foreground": [[30, 60, 210], [50, 80, 180]],
        "background": [[190, 40, 20], [160, 70, 40]],
    }


def test_semantic_failure_writes_json_without_fake_palette(tmp_path, monkeypatch):
    metadata = {
        "filename": "session.mp4",
        "media_id": "game_test_12345678",
        "media_type": "gameplay",
    }
    source_path = tmp_path / "source.png"
    Image.new("RGB", (64, 48), (10, 20, 30)).save(source_path)
    monkeypatch.setattr(
        palette_mod, "resolve_thumbnail_path", lambda *_args: source_path
    )
    monkeypatch.setattr(
        palette_mod, "_resolve_thumbnail_annotation", lambda *_args: SEMANTIC_ANNOTATION
    )
    monkeypatch.setattr(
        palette_mod,
        "_extract_fg_bg_full",
        MagicMock(side_effect=PaletteAnalysisError("semantic masks unavailable")),
    )
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_args: object()
    )

    with pytest.raises(PaletteAnalysisError, match="semantic masks unavailable"):
        palette_mod.create_thumbnail_palette(
            str(tmp_path),
            metadata["filename"],
            "gameplay",
            metadata=metadata,
            visualize=True,
        )

    output_dir = tmp_path / "outputs" / "test" / "palette"
    document = json.loads(
        (output_dir / "game_test_12345678.json").read_text(encoding="utf-8")
    )
    assert document == {
        "status": "failed",
        "media_id": "game_test_12345678",
        "filename": "session.mp4",
        "media_type": "gameplay",
        "error": "semantic masks unavailable",
    }
    assert not (output_dir / "game_test_12345678_palette.png").exists()


def test_visualization_reuses_single_semantic_inference(tmp_path, monkeypatch):
    metadata = {
        "filename": "session.mp4",
        "media_id": "game_test_12345678",
        "media_type": "gameplay",
    }
    source_path = tmp_path / "source.png"
    Image.new("RGB", (64, 48), (80, 100, 120)).save(source_path)

    class CountingSegmenter:
        model_name = "semantic-test"

        def __init__(self):
            self.calls = 0

        def segment_concept(self, image, concept):
            self.calls += 1
            assert concept == "subject"
            mask = np.zeros((image.height, image.width), dtype=bool)
            mask[64:192, 64:192] = True
            return [{"segmentation": mask, "bbox": [64, 64, 128, 128]}]

    segmenter = CountingSegmenter()
    monkeypatch.setattr(
        palette_mod, "resolve_thumbnail_path", lambda *_args: source_path
    )
    monkeypatch.setattr(
        palette_mod, "_resolve_thumbnail_annotation", lambda *_args: SEMANTIC_ANNOTATION
    )
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_args: segmenter
    )

    palette_mod.create_thumbnail_palette(
        str(tmp_path),
        metadata["filename"],
        "gameplay",
        metadata=metadata,
        visualize=True,
    )

    assert segmenter.calls == 1
    assert (
        tmp_path / "outputs" / "test" / "palette"
        / "game_test_12345678_semantic.png"
    ).exists()
    assert (
        tmp_path / "outputs" / "test" / "palette"
        / "game_test_12345678_seeds.png"
    ).exists()
    assert (
        tmp_path / "outputs" / "test" / "palette"
        / "game_test_12345678_clusters.png"
    ).exists()


def test_semantic_target_experiment_is_isolated_and_does_not_write_cache(
    tmp_path, monkeypatch,
):
    metadata = {
        "filename": "session.mp4",
        "media_id": "game_test_12345678",
        "media_type": "gameplay",
    }
    source_path = tmp_path / "source.png"
    Image.new("RGB", (64, 48), (80, 100, 120)).save(source_path)

    class TargetSegmenter:
        model_name = "semantic-test"

        def __init__(self):
            self.concepts = []

        def segment_concept(self, image, concept):
            self.concepts.append(concept)
            mask = np.zeros((image.height, image.width), dtype=bool)
            mask[64:192, 64:192] = True
            return [{"segmentation": mask, "bbox": [64, 64, 128, 128]}]

    segmenter = TargetSegmenter()
    save_cache = MagicMock()
    monkeypatch.setattr(
        palette_mod, "resolve_thumbnail_path", lambda *_args: source_path
    )
    monkeypatch.setattr(palette_mod, "save_thumbnail_palette", save_cache)

    result = palette_mod.create_thumbnail_palette_semantic_diagnostic(
        str(tmp_path),
        metadata["filename"],
        "gameplay",
        semantic_target="man",
        concepts=["man"],
        metadata=metadata,
        segmenter=segmenter,
    )

    assert segmenter.concepts == ["man"]
    save_cache.assert_not_called()
    output_dir = tmp_path / "outputs" / "test" / "palette"
    for suffix in (
        "_semantic.png",
        "_seeds.png",
        "_seeds_assignment.png",
        "_clusters.png",
        "_palette.png",
        ".json",
    ):
        assert (output_dir / f"game_test_12345678_man{suffix}").exists()
    document = json.loads(
        (output_dir / "game_test_12345678_man.json").read_text(encoding="utf-8")
    )
    assert document["semantic_target"] == "man"
    assert document["concepts_requested"] == ["man"]
    assert document["number_of_masks"] == 1
    assert document["seeds_count"] > 0
    assert document["foreground_superpixel_count"] > 0
    assert document["background_superpixel_count"] > 0
    assert document["ward_candidate_colors"]["foreground"]
    assert document["final_foreground_rgb"] == result["foreground"]["rgb"]
    assert document["final_background_rgb"] == result["background"]["rgb"]


def test_failed_semantic_target_records_empty_pipeline_without_fake_images(
    tmp_path, monkeypatch,
):
    metadata = {
        "filename": "session.mp4",
        "media_id": "game_test_12345678",
        "media_type": "gameplay",
    }
    source_path = tmp_path / "source.png"
    Image.new("RGB", (64, 48), (80, 100, 120)).save(source_path)
    segmenter = SimpleNamespace(
        model_name="semantic-test",
        segment_concept=lambda _image, _concept: [],
    )
    monkeypatch.setattr(
        palette_mod, "resolve_thumbnail_path", lambda *_args: source_path
    )

    with pytest.raises(PaletteAnalysisError, match="no usable masks"):
        palette_mod.create_thumbnail_palette_semantic_diagnostic(
            str(tmp_path),
            metadata["filename"],
            "gameplay",
            semantic_target="group",
            concepts=["group"],
            metadata=metadata,
            segmenter=segmenter,
        )

    output_dir = tmp_path / "outputs" / "test" / "palette"
    document = json.loads(
        (output_dir / "game_test_12345678_group.json").read_text(encoding="utf-8")
    )
    assert document["status"] == "failed"
    assert document["number_of_masks"] == 0
    assert document["semantic_coverage"] == 0.0
    assert document["seeds_count"] is None
    assert document["ward_candidate_colors"] == {
        "foreground": [], "background": [],
    }
    assert document["final_foreground_rgb"] is None
    assert not (output_dir / "game_test_12345678_group_palette.png").exists()


def test_non_visualized_creation_keeps_existing_extractor_call(tmp_path, monkeypatch):
    metadata = {
        "filename": "session.mp4",
        "media_id": "game_test_12345678",
        "media_type": "gameplay",
    }
    source_path = tmp_path / "source.png"
    Image.new("RGB", (64, 48), (80, 100, 120)).save(source_path)
    extractor = MagicMock(return_value=(FOREGROUND, BACKGROUND, _diagnostics(64, 48)))
    monkeypatch.setattr(
        palette_mod, "resolve_thumbnail_path", lambda *_args: source_path
    )
    monkeypatch.setattr(
        palette_mod, "_resolve_thumbnail_annotation", lambda *_args: SEMANTIC_ANNOTATION
    )
    monkeypatch.setattr(palette_mod, "_extract_fg_bg_full", extractor)
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_args: object()
    )

    palette_mod.create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "gameplay", metadata=metadata
    )

    assert extractor.call_args.kwargs == {
        "annotation": SEMANTIC_ANNOTATION,
        "segmenter": extractor.call_args.kwargs["segmenter"],
    }
    assert "diagnostic_capture" not in extractor.call_args.kwargs
    assert not (tmp_path / "outputs" / "test" / "palette").exists()


def test_visualize_does_not_recompute_current_cache_without_force(
    tmp_path, monkeypatch,
):
    metadata = {
        "filename": "session.mp4",
        "media_id": "game_test_12345678",
        "media_type": "gameplay",
    }
    source_path = tmp_path / "source.png"
    Image.new("RGB", (64, 48), (80, 100, 120)).save(source_path)
    extractor = MagicMock(return_value=(FOREGROUND, BACKGROUND, _diagnostics(64, 48)))
    monkeypatch.setattr(
        palette_mod, "resolve_thumbnail_path", lambda *_args: source_path
    )
    monkeypatch.setattr(
        palette_mod, "_resolve_thumbnail_annotation", lambda *_args: SEMANTIC_ANNOTATION
    )
    monkeypatch.setattr(palette_mod, "_extract_fg_bg_full", extractor)
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_args: object()
    )

    palette_mod.create_thumbnail_palette(
        str(tmp_path), metadata["filename"], "gameplay", metadata=metadata
    )
    summary = palette_mod.create_thumbnail_palette(
        str(tmp_path),
        metadata["filename"],
        "gameplay",
        metadata=metadata,
        visualize=True,
    )

    assert summary["cached"] is True
    assert extractor.call_count == 1
    assert not (tmp_path / "outputs" / "test" / "palette").exists()


def test_visualize_flag_parses_and_is_forwarded_for_title(tmp_path, monkeypatch, capsys):
    args = cli_mod.build_parser().parse_args(
        (
            "index palette create --thumbnail --title Session "
            "--force --visualize --verbose"
        ).split()
    )
    assert args.visualize is True

    create = MagicMock(return_value={
        "filename": "session.mp4",
        "processed": 1,
        "skipped": 0,
        "failed": 0,
        "cached": False,
    })
    monkeypatch.setattr(cli_mod.prefs, "get", lambda _key: str(tmp_path))
    monkeypatch.setattr(
        "data.shotlist.resolve_filename", lambda *_args: "session.mp4"
    )
    monkeypatch.setattr(palette_mod, "create_thumbnail_palette", create)

    cli_mod._index_palette_create(args)
    capsys.readouterr()

    assert create.call_args.kwargs["visualize"] is True
    assert create.call_args.kwargs["force"] is True
    assert create.call_args.kwargs["verbose"] is True


def test_visualize_is_forwarded_for_tmdb_selector(tmp_path, monkeypatch, capsys):
    args = cli_mod.build_parser().parse_args(
        "index palette create --thumbnail --tmdb 123 --visualize".split()
    )
    create = MagicMock(return_value={
        "filename": "session.mp4",
        "processed": 1,
        "skipped": 0,
        "failed": 0,
        "cached": False,
    })
    monkeypatch.setattr(cli_mod.prefs, "get", lambda _key: str(tmp_path))
    monkeypatch.setattr(
        "data.shotlist.resolve_filename", lambda *_args: "session.mp4"
    )
    monkeypatch.setattr(palette_mod, "create_thumbnail_palette", create)

    cli_mod._index_palette_create(args)
    capsys.readouterr()

    assert create.call_args.kwargs["visualize"] is True


def test_visualize_is_forwarded_for_all_gameplay(tmp_path, monkeypatch, capsys):
    args = cli_mod.build_parser().parse_args(
        "index palette create --thumbnail --all --media gameplay --visualize".split()
    )
    create_all = MagicMock(return_value={
        "media_type": "gameplay",
        "total_files": 0,
        "total_processed": 0,
        "total_skipped": 0,
        "total_failed": 0,
        "total_cached": 0,
        "results": [],
    })
    monkeypatch.setattr(cli_mod.prefs, "get", lambda _key: str(tmp_path))
    monkeypatch.setattr(
        palette_mod, "create_thumbnail_palettes_for_all", create_all
    )

    cli_mod._index_palette_create(args)
    capsys.readouterr()

    assert create_all.call_args.args[:2] == (str(tmp_path), "gameplay")
    assert create_all.call_args.kwargs["visualize"] is True


def test_visualize_requires_thumbnail(capsys):
    args = cli_mod.build_parser().parse_args(
        "index palette create --title Session --visualize".split()
    )

    with pytest.raises(SystemExit):
        cli_mod._index_palette_create(args)

    assert "--visualize requires --thumbnail" in capsys.readouterr().err