import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

import cli as cli_mod
import data.palette as palette_mod
from data.palette import (
    PaletteAnalysisError,
    _extract_fg_bg_figure,
    create_palette_for_all_movies,
    create_palette_for_movie,
    create_thumbnail_palette,
    get_palette_path,
    get_thumbnail_palette_path,
    save_palette,
    save_thumbnail_palette,
)


class ValidSegmenter:
    model_name = "sam3-test"

    def segment_palette(self, image):
        height, width = np.asarray(image).shape[:2]
        top, bottom = height // 4, height - height // 4
        left, right = width // 4, width - width // 4
        mask = np.zeros((height, width), dtype=bool)
        mask[top:bottom, left:right] = True
        return [{
            "segmentation": mask,
            "bbox": [left, top, right - left, bottom - top],
        }]


class FailingSegmenter:
    def segment_palette(self, _image):
        raise RuntimeError("model crashed")


class EmptySegmenter:
    def segment_palette(self, _image):
        return []


class MalformedSegmenter:
    def segment_palette(self, _image):
        return [{"segmentation": np.ones((2, 2), dtype=bool), "bbox": [0, 0, 2, 2]}]


def _image_array():
    image = np.full((256, 256, 3), (30, 40, 80), dtype=np.uint8)
    image[64:192, 64:192] = (200, 80, 30)
    return image


def _metadata():
    return {
        "filename": "The Searchers (1956).mp4",
        "media_type": "movie",
        "media_id": "tmdb_3114",
        "title": "The Searchers",
        "year": 1956,
        "tmdb": "3114",
    }


def _write_thumbnail(project: Path, metadata: dict) -> Path:
    path = (
        project / "media" / "thumbnails" / metadata["media_type"]
        / f"{Path(metadata['filename']).stem}.jpg"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (120, 60, 20)).save(path)
    return path


def _prepare_best_frame(project: Path, monkeypatch):
    from services.frame_match import best_frame_path

    metadata = _metadata()
    shot_id = "tmdb_3114@f000000-f000100"
    frame_path = best_frame_path(
        str(project), "movie", metadata["filename"], shot_id
    )
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (120, 60, 20)).save(frame_path)
    entry = {
        "movie": {"filename": metadata["filename"]},
        "shot": {
            "shot_id": shot_id,
            "best_frame": {"frame": 50, "score": 0.9, "method": "clip"},
        },
    }
    monkeypatch.setattr(palette_mod, "get_metadata", lambda *_a, **_k: [metadata])
    monkeypatch.setattr(palette_mod, "load_annotation_items", lambda *_a: [entry])
    monkeypatch.setattr(
        palette_mod, "read_shotlist", lambda *_a: (_ for _ in ()).throw(FileNotFoundError())
    )
    return metadata


def test_successful_configured_segmentation_produces_palette():
    foreground, background, diagnostics = _extract_fg_bg_figure(
        _image_array(), segmenter=ValidSegmenter()
    )

    assert foreground["palette"]
    assert background["palette"]
    assert diagnostics["segmentation_used"] == "sam3-test"
    assert diagnostics["fallback_level"] == 0


def test_segmentation_unavailable_is_explicit_failure():
    with pytest.raises(PaletteAnalysisError, match="segmentation unavailable"):
        _extract_fg_bg_figure(_image_array(), segmenter=None)


def test_segmentation_failure_is_explicit_failure():
    with pytest.raises(PaletteAnalysisError, match="segmentation failed.*model crashed"):
        _extract_fg_bg_figure(_image_array(), segmenter=FailingSegmenter())


def test_empty_segmentation_is_explicit_failure():
    with pytest.raises(PaletteAnalysisError, match="no usable foreground"):
        _extract_fg_bg_figure(_image_array(), segmenter=EmptySegmenter())


def test_malformed_segmentation_is_explicit_failure():
    with pytest.raises(PaletteAnalysisError, match="returned mask shape"):
        _extract_fg_bg_figure(_image_array(), segmenter=MalformedSegmenter())


def test_spatial_center_border_fallback_is_absent():
    assert not hasattr(palette_mod, "_spatial_masks")
    with pytest.raises(PaletteAnalysisError):
        _extract_fg_bg_figure(_image_array(), segmenter=None)


def test_configured_sam3_adapter_produces_foreground_through_canonical_interface(
    monkeypatch,
):
    import torch

    from services.silhouette import (
        PALETTE_SEGMENTATION_CONCEPT,
        _SAM3Adapter,
    )

    mask = torch.zeros((1, 256, 256), dtype=torch.bool)
    mask[:, 64:192, 64:192] = True
    seen = {}

    class FakeModel:
        def __call__(self, **_inputs):
            return object()

    class FakeProcessor:
        def __call__(self, *, images, text, return_tensors):
            seen["image_size"] = images.size
            seen["concept"] = text
            assert return_tensors == "pt"
            return {}

        def post_process_instance_segmentation(
            self, _outputs, *, threshold, mask_threshold, target_sizes,
        ):
            assert threshold == 0.3
            assert mask_threshold == 0.5
            assert target_sizes == [(256, 256)]
            return [{
                "masks": mask,
                "scores": torch.tensor([0.95]),
                "boxes": torch.tensor([[64, 64, 192, 192]]),
            }]

    adapter = _SAM3Adapter(FakeModel(), FakeProcessor(), "cpu", "sam3")
    load_calls = []
    monkeypatch.setattr("tool.prefs.get", lambda key: "sam3" if key == "model_segmentation" else None)
    monkeypatch.setattr(
        "services.silhouette.load_sam_model",
        lambda project, model: load_calls.append((project, model))
        or (adapter, model, "cpu"),
    )

    segmenter = palette_mod._load_palette_segmenter("/project")
    foreground, background, diagnostics = _extract_fg_bg_figure(
        _image_array(), segmenter=segmenter
    )

    assert load_calls == [("/project", "sam3")]
    assert seen == {
        "image_size": (256, 256),
        "concept": PALETTE_SEGMENTATION_CONCEPT,
    }
    assert foreground["coverage"] > 0
    assert background["coverage"] > 0
    assert diagnostics["segmentation_used"] == "sam3"


def test_configured_incompatible_model_is_explicitly_unavailable(monkeypatch):
    class ConceptOnlySegmenter:
        def segment_concept(self, _image, _concept):
            return []

    monkeypatch.setattr("tool.prefs.get", lambda _key: "sam3-model")
    monkeypatch.setattr(
        "services.silhouette.load_sam_model",
        lambda *_args: (ConceptOnlySegmenter(), "sam3-model", "cpu"),
    )

    with pytest.raises(PaletteAnalysisError, match="does not expose segment_palette"):
        palette_mod._load_palette_segmenter("/project")


def test_failed_best_frame_analysis_writes_no_cache(tmp_path, monkeypatch):
    metadata = _prepare_best_frame(tmp_path, monkeypatch)
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_a: FailingSegmenter()
    )

    with pytest.raises(PaletteAnalysisError, match="model crashed"):
        create_palette_for_movie(
            str(tmp_path), metadata["filename"], "movie", force=True
        )

    assert not get_palette_path(
        str(tmp_path), metadata["filename"], "movie"
    ).exists()


def test_failed_best_frame_rebuild_preserves_valid_cache(tmp_path, monkeypatch):
    metadata = _prepare_best_frame(tmp_path, monkeypatch)
    original = {"source": "best_frame", "shots": [{"foreground": {"rgb": [1, 2, 3]}}]}
    cache_path = save_palette(
        str(tmp_path), metadata["filename"], "movie", original
    )
    original_bytes = cache_path.read_bytes()
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_a: FailingSegmenter()
    )

    with pytest.raises(PaletteAnalysisError, match="model crashed"):
        create_palette_for_movie(
            str(tmp_path), metadata["filename"], "movie", force=True
        )

    assert cache_path.read_bytes() == original_bytes


def test_failed_thumbnail_analysis_writes_no_cache(tmp_path, monkeypatch):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_a: FailingSegmenter()
    )

    with pytest.raises(PaletteAnalysisError, match="model crashed"):
        create_thumbnail_palette(
            str(tmp_path), metadata["filename"], "movie", metadata=metadata
        )

    assert not get_thumbnail_palette_path(
        str(tmp_path), metadata["media_id"], "movie"
    ).exists()


def test_invalid_thumbnail_image_writes_no_cache(tmp_path, monkeypatch):
    metadata = _metadata()
    thumbnail = _write_thumbnail(tmp_path, metadata)
    thumbnail.write_text("not an image", encoding="utf-8")
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_a: ValidSegmenter()
    )

    with pytest.raises(PaletteAnalysisError, match="source image could not be analyzed"):
        create_thumbnail_palette(
            str(tmp_path), metadata["filename"], "movie", metadata=metadata
        )

    assert not get_thumbnail_palette_path(
        str(tmp_path), metadata["media_id"], "movie"
    ).exists()


def test_failed_thumbnail_rebuild_preserves_valid_cache(tmp_path, monkeypatch):
    metadata = _metadata()
    _write_thumbnail(tmp_path, metadata)
    original = {
        "schema_version": 1,
        "source": "thumbnail",
        "media": metadata,
        "thumbnail": {"foreground": {"rgb": [1, 2, 3]}},
    }
    cache_path = save_thumbnail_palette(
        str(tmp_path), metadata["media_id"], "movie", original
    )
    original_bytes = cache_path.read_bytes()
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_a: FailingSegmenter()
    )

    with pytest.raises(PaletteAnalysisError, match="model crashed"):
        create_thumbnail_palette(
            str(tmp_path), metadata["filename"], "movie",
            metadata=metadata, force=True,
        )

    assert cache_path.read_bytes() == original_bytes


def test_best_frame_batch_counts_and_reports_analysis_failure(
    tmp_path, monkeypatch, capsys,
):
    metadata = _metadata()
    monkeypatch.setattr(palette_mod, "get_metadata", lambda *_a, **_k: [metadata])
    monkeypatch.setattr(
        palette_mod,
        "create_palette_for_movie",
        lambda *_a, **_k: (_ for _ in ()).throw(
            PaletteAnalysisError("Palette segmentation unavailable")
        ),
    )

    summary = create_palette_for_all_movies(str(tmp_path), "movie")

    assert summary["total_failed"] == 1
    assert summary["results"] == [{
        "filename": metadata["filename"],
        "error": "Palette segmentation unavailable",
    }]
    assert (
        "fail  The Searchers (1956).mp4: Palette segmentation unavailable"
        in capsys.readouterr().out
    )


def test_cli_batch_failure_is_visible_and_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_mod.prefs, "get", lambda _key: str(tmp_path))
    monkeypatch.setattr(
        palette_mod,
        "create_thumbnail_palettes_for_all",
        lambda *_a, **_k: {
            "media_type": "movie",
            "total_files": 1,
            "total_processed": 0,
            "total_skipped": 0,
            "total_failed": 1,
            "total_cached": 0,
            "results": [{
                "filename": "The Searchers.mp4",
                "error": "Palette segmentation unavailable",
            }],
        },
    )
    args = SimpleNamespace(
        media="movie", force=False, verbose=False, notify_items=False,
        notify=False, all=True, movie=None, tmdb=None, thumbnail=True,
    )

    with pytest.raises(SystemExit) as exc:
        cli_mod._index_palette_create(args)

    assert exc.value.code == 1
    assert "processed=0  skipped=0  failed=1" in capsys.readouterr().out