from types import SimpleNamespace

import numpy as np
import pytest

import data.palette as palette_mod
from data.palette import PaletteAnalysisError


class SemanticSegmenter:
    model_name = "sam3-test"

    def __init__(self, masks_by_concept):
        self.masks_by_concept = masks_by_concept
        self.calls = []

    def segment_concept(self, image, concept):
        self.calls.append((image.size, concept))
        result = self.masks_by_concept.get(concept, [])
        if isinstance(result, Exception):
            raise result
        return [
            {
                "segmentation": np.asarray(mask, dtype=bool).copy(),
                "bbox": [0, 0, mask.shape[1], mask.shape[0]],
            }
            for mask in result
        ]


def _mask(*rows):
    return np.asarray(rows, dtype=bool)


def _annotation(**overrides):
    result = {
        "humans": [],
        "animals": [],
        "objects": [],
        "setting": "",
    }
    result.update(overrides)
    return result


def _select(annotation, masks_by_concept):
    segmenter = SemanticSegmenter(masks_by_concept)
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    foreground, background, diagnostics = (
        palette_mod._select_semantic_foreground(
            image, annotation=annotation, segmenter=segmenter
        )
    )
    return foreground, background, diagnostics, segmenter


def test_humans_are_selected_as_semantic_foreground():
    man = _mask(
        [1, 1, 0, 0],
        [1, 1, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
    )

    foreground, _, diagnostics, segmenter = _select(
        _annotation(humans=["man"]), {"man": [man]}
    )

    np.testing.assert_array_equal(foreground, man)
    assert segmenter.calls == [((4, 4), "man")]
    assert diagnostics["foreground_mode"] == "semantic"
    assert diagnostics["semantic_categories_used"] == ["humans"]
    assert diagnostics["semantic_concepts_used"] == ["man"]


def test_animals_are_used_when_humans_are_absent():
    horse = _mask(
        [0, 0, 1, 1],
        [0, 0, 1, 1],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
    )

    foreground, _, diagnostics, segmenter = _select(
        _annotation(animals=["horse"]), {"horse": [horse]}
    )

    np.testing.assert_array_equal(foreground, horse)
    assert segmenter.calls == [((4, 4), "horse")]
    assert diagnostics["semantic_categories_used"] == ["animals"]


def test_objects_are_used_when_humans_and_animals_are_absent():
    rifle = _mask(
        [0, 0, 0, 0],
        [0, 1, 1, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
    )

    foreground, _, diagnostics, segmenter = _select(
        _annotation(objects=["rifle"]), {"rifle": [rifle]}
    )

    np.testing.assert_array_equal(foreground, rifle)
    assert segmenter.calls == [((4, 4), "rifle")]
    assert diagnostics["semantic_categories_used"] == ["objects"]


def test_all_categories_preserve_priority_for_overlap_ownership():
    man = _mask(
        [1, 1, 1, 0],
        [1, 1, 1, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
    )
    horse = _mask(
        [0, 1, 1, 1],
        [0, 1, 1, 1],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
    )
    wagon = _mask(
        [0, 0, 0, 0],
        [0, 0, 1, 1],
        [0, 0, 1, 1],
        [0, 0, 0, 0],
    )

    foreground, _, diagnostics, segmenter = _select(
        _annotation(
            humans=["man"], animals=["horse"], objects=["wagon"]
        ),
        {"man": [man], "horse": [horse], "wagon": [wagon]},
    )

    np.testing.assert_array_equal(foreground, man | horse | wagon)
    assert [concept for _, concept in segmenter.calls] == [
        "man", "horse", "wagon"
    ]
    assert diagnostics["semantic_categories_used"] == [
        "humans", "animals", "objects"
    ]
    assert diagnostics["semantic_category_pixel_counts"] == {
        "humans": 6,
        "animals": 2,
        "objects": 2,
    }


def test_setting_only_uses_explicit_setting_mode():
    forest = _mask(
        [1, 1, 1, 1],
        [1, 1, 1, 1],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
    )

    foreground, _, diagnostics, segmenter = _select(
        _annotation(setting="forest"), {"forest": [forest]}
    )

    np.testing.assert_array_equal(foreground, forest)
    assert segmenter.calls == [((4, 4), "forest")]
    assert diagnostics["foreground_mode"] == "setting"
    assert diagnostics["semantic_categories_used"] == ["setting"]
    assert diagnostics["semantic_concepts_used"] == ["forest"]


def test_all_empty_semantic_categories_fail_explicitly():
    segmenter = SemanticSegmenter({})

    with pytest.raises(
        PaletteAnalysisError,
        match="No semantic image categories available for palette extraction",
    ):
        palette_mod._select_semantic_foreground(
            np.zeros((4, 4, 3), dtype=np.uint8),
            annotation=_annotation(),
            segmenter=segmenter,
        )

    assert segmenter.calls == []


def test_semantic_mask_failure_is_explicit():
    segmenter = SemanticSegmenter({"man": RuntimeError("model crashed")})

    with pytest.raises(
        PaletteAnalysisError,
        match="Semantic palette segmentation failed.*man.*model crashed",
    ):
        palette_mod._select_semantic_foreground(
            np.zeros((4, 4, 3), dtype=np.uint8),
            annotation=_annotation(humans=["man"]),
            segmenter=segmenter,
        )


def test_empty_semantic_masks_do_not_synthesize_spatial_foreground():
    segmenter = SemanticSegmenter({"man": []})

    with pytest.raises(
        PaletteAnalysisError,
        match="no usable masks.*man",
    ):
        palette_mod._select_semantic_foreground(
            np.zeros((4, 4, 3), dtype=np.uint8),
            annotation=_annotation(humans=["man"]),
            segmenter=segmenter,
        )

    assert not hasattr(palette_mod, "_spatial_masks")


def test_semantic_diagnostics_report_masks_and_coverage():
    man = _mask(
        [1, 1, 0, 0],
        [1, 1, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
    )

    _, background, diagnostics, _ = _select(
        _annotation(humans=["man", "man", " woman "]),
        {"man": [man], "woman": [man]},
    )

    assert diagnostics["semantic_concepts_used"] == ["man", "woman"]
    assert diagnostics["semantic_mask_count"] == 2
    assert diagnostics["semantic_foreground_coverage"] == 0.25
    assert int(background.sum()) == 12


def test_semantic_selection_is_deterministic():
    man = _mask(
        [1, 0, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
    )
    annotation = _annotation(
        humans=["man", "man"], animals=["horse"], setting="town"
    )
    masks = {"man": [man], "horse": [man]}

    first = _select(annotation, masks)
    second = _select(annotation, masks)

    np.testing.assert_array_equal(first[0], second[0])
    np.testing.assert_array_equal(first[1], second[1])
    assert first[2] == second[2]
    assert first[3].calls == second[3].calls


def test_thumbnail_and_best_frame_pass_annotations_to_shared_extractor(
    monkeypatch, tmp_path,
):
    annotation = _annotation(humans=["man"])
    image_path = tmp_path / "source.jpg"
    image_path.write_bytes(b"source")
    calls = []

    def extract(path, *, annotation, segmenter):
        calls.append((path, annotation, segmenter))
        return ({"rgb": [1, 2, 3]}, {"rgb": [4, 5, 6]}, {})

    segmenter = object()
    metadata = {
        "filename": "film.mp4",
        "media_id": "film_id",
        "media_type": "movie",
    }
    monkeypatch.setattr(palette_mod, "resolve_thumbnail_path", lambda *_a: image_path)
    monkeypatch.setattr(
        palette_mod, "_thumbnail_source_fingerprint", lambda *_a: {"path": "source.jpg", "sha256": "hash"}
    )
    monkeypatch.setattr(
        palette_mod, "_resolve_thumbnail_annotation", lambda *_a: annotation
    )
    monkeypatch.setattr(palette_mod, "_extract_fg_bg_full", extract)
    monkeypatch.setattr(
        palette_mod, "_load_palette_segmenter", lambda *_a: segmenter
    )
    monkeypatch.setattr(
        palette_mod, "save_thumbnail_palette", lambda *_a, **_k: tmp_path / "cache.json"
    )
    monkeypatch.setattr(
        "services.frame_match.best_frame_path", lambda *_a: image_path
    )

    palette_mod.create_thumbnail_palette(
        str(tmp_path), "film.mp4", metadata=metadata
    )
    palette_mod._process_one_shot(
        str(tmp_path),
        "film.mp4",
        "movie",
        {"shot": {
            "shot_id": "film_id@f000000-f000010",
            "annotation": annotation,
            "best_frame": {"frame": 5},
        }},
        0,
        None,
        segmenter=segmenter,
    )

    assert calls == [
        (image_path, annotation, segmenter),
        (image_path, annotation, segmenter),
    ]


def test_legacy_thumbnail_cache_is_not_current_semantic_analysis():
    source = {"path": "thumbnail.jpg", "sha256": "same"}
    semantic_input = palette_mod._semantic_input_fingerprint(
        _annotation(humans=["man"])
    )
    legacy = {
        "schema_version": 1,
        "source": "thumbnail",
        "source_image": source,
        "method": "figure",
    }

    assert palette_mod._thumbnail_cache_is_current(
        legacy, source, semantic_input
    ) is False


def test_legacy_best_frame_cache_is_not_current_semantic_analysis():
    legacy = {
        "schema_version": 1,
        "source": "best_frame",
        "method": "figure",
    }

    assert palette_mod._semantic_cache_is_current(legacy) is False


def test_semantic_thumbnail_cache_is_current_for_same_source():
    source = {"path": "thumbnail.jpg", "sha256": "same"}
    semantic_input = palette_mod._semantic_input_fingerprint(
        _annotation(humans=["man"])
    )
    semantic = {
        "schema_version": palette_mod.PALETTE_SCHEMA_VERSION,
        "analysis_version": palette_mod.PALETTE_ANALYSIS_VERSION,
        "source": "thumbnail",
        "source_image": source,
        "semantic_input": semantic_input,
        "method": "semantic-figure",
    }

    assert palette_mod._thumbnail_cache_is_current(
        semantic, source, semantic_input
    ) is True


def test_semantic_thumbnail_cache_is_stale_when_annotation_changes():
    source = {"path": "thumbnail.jpg", "sha256": "same"}
    cached_input = palette_mod._semantic_input_fingerprint(
        _annotation(humans=["man"])
    )
    current_input = palette_mod._semantic_input_fingerprint(
        _annotation(animals=["horse"])
    )
    semantic = {
        "schema_version": palette_mod.PALETTE_SCHEMA_VERSION,
        "analysis_version": palette_mod.PALETTE_ANALYSIS_VERSION,
        "source": "thumbnail",
        "source_image": source,
        "semantic_input": cached_input,
        "method": "semantic-figure",
    }

    assert palette_mod._thumbnail_cache_is_current(
        semantic, source, current_input
    ) is False


def test_gameplay_thumbnail_annotation_resolves_canonical_timestamp_shot(
    monkeypatch, tmp_path,
):
    filename = "session.mp4"
    media_id = "game_rdr2_abc12345"
    video_path = tmp_path / "media" / "videos" / "gameplay" / filename
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    annotation = _annotation(humans=["man"], setting="forest")
    shot_id = f"{media_id}@f000100-f000200"

    monkeypatch.setattr(
        "services.transcode.get_video_thumbnail_timestamp", lambda _path: 5.0
    )
    monkeypatch.setattr(
        palette_mod,
        "read_shotlist",
        lambda *_args: [{
            "start_time": "00:00:05.000",
            "end_time": "00:00:06.000",
            "start_frame": "100",
            "end_frame": "200",
        }],
    )
    monkeypatch.setattr(
        palette_mod,
        "load_annotation_items",
        lambda *_args: [{
            "shot": {"shot_id": shot_id, "annotation": annotation}
        }],
    )

    resolved = palette_mod._resolve_thumbnail_annotation(
        str(tmp_path), filename, "gameplay", media_id, {}
    )

    assert resolved == annotation


def test_movie_thumbnail_without_per_image_annotation_fails_explicitly(tmp_path):
    with pytest.raises(
        PaletteAnalysisError,
        match="No semantic image categories available.*no per-image shot annotation",
    ):
        palette_mod._resolve_thumbnail_annotation(
            str(tmp_path), "film.mp4", "movie", "tmdb_1", {}
        )


@pytest.mark.parametrize("missing_source", ["shotlist", "annotation"])
def test_gameplay_thumbnail_missing_provenance_fails_explicitly(
    missing_source, monkeypatch, tmp_path,
):
    filename = "session.mp4"
    media_id = "game_rdr2_abc12345"
    video_path = tmp_path / "media" / "videos" / "gameplay" / filename
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    monkeypatch.setattr(
        "services.transcode.get_video_thumbnail_timestamp", lambda _path: 5.0
    )

    if missing_source == "shotlist":
        monkeypatch.setattr(
            palette_mod,
            "read_shotlist",
            lambda *_args: (_ for _ in ()).throw(FileNotFoundError("no shotlist")),
        )
    else:
        monkeypatch.setattr(
            palette_mod,
            "read_shotlist",
            lambda *_args: [{
                "start_time": "00:00:04.000",
                "end_time": "00:00:06.000",
                "start_frame": "100",
                "end_frame": "200",
            }],
        )
        monkeypatch.setattr(palette_mod, "load_annotation_items", lambda *_args: [])

    with pytest.raises(
        PaletteAnalysisError,
        match="No semantic image categories available",
    ):
        palette_mod._resolve_thumbnail_annotation(
            str(tmp_path), filename, "gameplay", media_id, {}
        )
