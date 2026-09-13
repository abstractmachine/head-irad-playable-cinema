"""Read-only exact silhouette reference packet tests."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from data.metadata import save_json_metadata
from data.shotlist import write_shotlist
from services.illustration_index import rebuild_index
from services import silhouette_reference as reference


_MEDIA_ID = "tmdb_1"
_FILENAME = "Film.mp4"
_SHOT_ID = "tmdb_1@f000100-f000140"


def _write_candidate(
    project_path: Path,
    *,
    index: int = 1,
    frame_index: int = 117,
    confidence: float = 0.2,
    mask_area: int = 100,
    assignment_state: str = "active",
    pixels: bytes = b"\x10\x20\x30\x80" * 12,
) -> tuple[Path, Path]:
    directory = (
        project_path / "data" / "silhouettes" / "catalog" / "movie" / "Film" / "gun"
    )
    directory.mkdir(parents=True, exist_ok=True)
    png_path = directory / f"object_{index:04d}.png"
    json_path = directory / f"object_{index:04d}.json"
    image = Image.frombytes("RGBA", (4, 3), pixels)
    image.save(png_path, format="PNG", optimize=False)
    json_path.write_text(json.dumps({
        "schema_version": "1",
        "media_type": "movie",
        "filename": _FILENAME,
        "filename_stem": "Film",
        "media_id": _MEDIA_ID,
        "shot_id": _SHOT_ID,
        "frame": frame_index,
        "label": "gun",
        "field": "objects",
        "confidence": confidence,
        "bbox": [1, 2, 3, 4],
        "mask_area": mask_area,
        "frame_size": [16, 9],
        "png": png_path.name,
        "assignment": {"state": assignment_state},
    }), encoding="utf-8")
    rebuild_index(project_path, "silhouettes", "movie")
    return json_path, png_path


def _seed_project(project_path: Path) -> None:
    save_json_metadata(str(project_path), "movie", [{
        "filename": _FILENAME,
        "title": "Film",
        "tmdb": 1,
    }])
    write_shotlist(str(project_path), _FILENAME, "movie", [{
        "Scene": "4",
        "start_time": "00:00:10.000",
        "end_time": "00:00:14.000",
        "start_frame": 100,
        "end_frame": 140,
        "shot_id": _SHOT_ID,
        "Shot_Caption": "A gun appears.",
        "Scene_Caption": "The standoff.",
    }])
    video_path = project_path / "media" / "videos" / "movie" / _FILENAME
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"placeholder video")


def _packet_kwargs(**overrides) -> dict:
    return {
        "media_type": "movie",
        "media_id": _MEDIA_ID,
        "shot_id": _SHOT_ID,
        "frame_index": 117,
        "word": "gun",
        "field": "objects",
        **overrides,
    }


def _patch_video(project_path: Path, monkeypatch, *, context_indices=None):
    from data import annotate
    from generators import mosaic
    from services import frame_match

    video_path = project_path / "media" / "videos" / "movie" / _FILENAME
    extracted = []

    def extract_frame(path, frame_index, *, apply_sample_aspect_ratio):
        assert path == video_path
        assert apply_sample_aspect_ratio is False
        extracted.append(frame_index)
        return Image.new("RGB", (16, 9), (frame_index % 256, 20, 30))

    monkeypatch.setattr(mosaic, "_find_video_path", lambda _project, _stem: video_path)
    monkeypatch.setattr(mosaic, "extract_frame_pil", extract_frame)
    monkeypatch.setattr(frame_match, "_get_video_fps", lambda _path: 10.0)
    if context_indices is not None:
        monkeypatch.setattr(
            annotate,
            "canonical_context_frame_indices",
            lambda *_args, **_kwargs: list(context_indices),
        )
    return extracted


def test_packet_returns_the_exact_selected_candidate_and_native_frames(tmp_path, monkeypatch):
    _seed_project(tmp_path)
    json_path, png_path = _write_candidate(tmp_path)
    extracted = _patch_video(tmp_path, monkeypatch, context_indices=[105, 117, 135])

    packet = reference.retrieve_silhouette_reference_packet(
        tmp_path, **_packet_kwargs(),
    )

    metadata = packet["metadata"]
    content = packet["content"]
    assert metadata["candidate_ref"]["candidate_id"] == str(json_path.relative_to(tmp_path))
    assert metadata["candidate_ref"]["object_id"] == "object_0001"
    assert metadata["candidate"]["silhouette_path"] == str(png_path.relative_to(tmp_path))
    assert metadata["candidate"]["frame_index"] == 117
    assert metadata["candidate"]["silhouette_width"] == 4
    assert metadata["candidate"]["silhouette_height"] == 3
    assert metadata["candidate"]["silhouette_mode"] == "RGBA"
    assert metadata["source_frame"]["frame_index"] == 117
    assert metadata["source_frame"]["is_exact_extraction_frame"] is True
    assert metadata["source_frame"]["width"] == 16
    assert metadata["source_frame"]["height"] == 9
    assert metadata["provenance"]["new_inference_performed"] is False
    assert metadata["provenance"]["selection_changed"] is False
    assert metadata["content_order"] == [
        "metadata", "silhouette", "exact_source_frame", "context_frame_01", "context_frame_02",
    ]
    assert [item["content_id"] for item in content] == metadata["content_order"][1:]
    assert content[0]["data"] == png_path.read_bytes()
    with Image.open(io.BytesIO(content[0]["data"])) as silhouette:
        assert silhouette.mode == "RGBA"
        assert silhouette.size == (4, 3)
        assert silhouette.getchannel("A").getdata()[0] == 128
    assert extracted == [117, 105, 135]
    assert [frame["frame_index"] for frame in metadata["context"]["frames"]] == [105, 117, 135]
    assert [frame["relative_frame"] for frame in metadata["context"]["frames"]] == [-12, 0, 18]
    assert metadata["context"]["frames"][1]["content_id"] == "exact_source_frame"


def test_candidate_id_selects_exact_object_without_ranking(tmp_path, monkeypatch):
    _seed_project(tmp_path)
    first_json, first_png = _write_candidate(tmp_path, index=1, confidence=0.1, mask_area=10)
    _write_candidate(tmp_path, index=2, confidence=0.99, mask_area=999)
    _patch_video(tmp_path, monkeypatch, context_indices=[])

    packet = reference.retrieve_silhouette_reference_packet(
        tmp_path,
        **_packet_kwargs(candidate_id=str(first_json.relative_to(tmp_path))),
    )

    assert packet["metadata"]["candidate"]["object_id"] == "object_0001"
    assert packet["metadata"]["candidate"]["score"] == 0.1
    assert packet["content"][0]["data"] == first_png.read_bytes()


def test_reference_packet_requires_active_index_candidate(tmp_path, monkeypatch):
    _seed_project(tmp_path)
    active_json, _active_png = _write_candidate(tmp_path, index=1)
    historical_json, _historical_png = _write_candidate(
        tmp_path, index=2, assignment_state="superseded",
    )
    _patch_video(tmp_path, monkeypatch, context_indices=[])
    monkeypatch.setattr(
        "services.silhouette_catalog.iter_catalog",
        lambda *_args, **_kwargs: pytest.fail("reference packets must not discover raw catalog records"),
    )

    packet = reference.retrieve_silhouette_reference_packet(
        tmp_path,
        **_packet_kwargs(candidate_id=str(active_json.relative_to(tmp_path))),
    )
    assert packet["metadata"]["candidate_ref"]["object_id"] == "object_0001"

    with pytest.raises(reference.SilhouetteReferenceError) as caught:
        reference.retrieve_silhouette_reference_packet(
            tmp_path,
            **_packet_kwargs(candidate_id=str(historical_json.relative_to(tmp_path))),
        )
    assert caught.value.code == "CANDIDATE_NOT_FOUND"
    assert caught.value.stage == "CANDIDATE_RESOLUTION"


def test_png_path_safely_disambiguates_two_same_shot_candidates(tmp_path, monkeypatch):
    _seed_project(tmp_path)
    _write_candidate(tmp_path, index=1)
    second_json, second_png = _write_candidate(tmp_path, index=2)
    _patch_video(tmp_path, monkeypatch, context_indices=[])

    packet = reference.retrieve_silhouette_reference_packet(
        tmp_path,
        **_packet_kwargs(png_path=str(second_png.relative_to(tmp_path))),
    )

    assert packet["metadata"]["candidate"]["candidate_id"] == str(
        second_json.relative_to(tmp_path)
    )
    assert packet["content"][0]["data"] == second_png.read_bytes()


def test_matching_multiple_candidates_is_an_explicit_ambiguity(tmp_path, monkeypatch):
    _seed_project(tmp_path)
    _write_candidate(tmp_path, index=1, confidence=0.1, mask_area=10)
    _write_candidate(tmp_path, index=2, confidence=0.99, mask_area=999)
    _patch_video(tmp_path, monkeypatch, context_indices=[])

    with pytest.raises(reference.SilhouetteReferenceError) as caught:
        reference.retrieve_silhouette_reference_packet(tmp_path, **_packet_kwargs())

    error = caught.value
    assert error.code == "CANDIDATE_AMBIGUOUS"
    assert error.stage == "CANDIDATE_RESOLUTION"
    assert [candidate["object_id"] for candidate in error.details["candidates"]] == [
        "object_0001", "object_0002",
    ]
    assert error.details["candidates"][1]["score"] == 0.99
    assert error.details["candidates"][1]["mask_area"] == 999


def test_packet_reports_not_found_and_rejects_escaping_png_paths(tmp_path, monkeypatch):
    _seed_project(tmp_path)
    _write_candidate(tmp_path)
    _patch_video(tmp_path, monkeypatch, context_indices=[])

    with pytest.raises(reference.SilhouetteReferenceError) as missing:
        reference.retrieve_silhouette_reference_packet(
            tmp_path, **_packet_kwargs(frame_index=118),
        )
    assert missing.value.code == "CANDIDATE_NOT_FOUND"
    assert missing.value.stage == "CANDIDATE_RESOLUTION"

    with pytest.raises(reference.SilhouetteReferenceError) as unsafe:
        reference.retrieve_silhouette_reference_packet(
            tmp_path, **_packet_kwargs(png_path="../../outside.png"),
        )
    assert unsafe.value.code == "CANDIDATE_PATH_INVALID"
    assert unsafe.value.stage == "CANDIDATE_RESOLUTION"

    json_path = tmp_path / "data" / "silhouettes" / "catalog" / "movie" / "Film" / "gun" / "object_0001.json"
    candidate = json.loads(json_path.read_text(encoding="utf-8"))
    candidate["png"] = "../other/object_0001.png"
    json_path.write_text(json.dumps(candidate), encoding="utf-8")
    rebuild_index(tmp_path, "silhouettes", "movie")
    with pytest.raises(reference.SilhouetteReferenceError) as malformed:
        reference.retrieve_silhouette_reference_packet(tmp_path, **_packet_kwargs())
    assert malformed.value.code == "SILHOUETTE_PATH_INVALID"
    assert malformed.value.stage == "SILHOUETTE_DECODING"


def test_short_shot_context_is_deduplicated_without_padding(tmp_path, monkeypatch):
    _seed_project(tmp_path)
    _write_candidate(tmp_path, frame_index=100)
    extracted = _patch_video(tmp_path, monkeypatch, context_indices=[100, 100])

    packet = reference.retrieve_silhouette_reference_packet(
        tmp_path, **_packet_kwargs(frame_index=100),
    )

    assert extracted == [100]
    assert [item["content_id"] for item in packet["content"]] == [
        "silhouette", "exact_source_frame",
    ]
    assert [frame["frame_index"] for frame in packet["metadata"]["context"]["frames"]] == [100]
    assert any("fewer than two" in warning for warning in packet["metadata"]["warnings"])


def test_canonical_context_indices_are_chronological_and_shot_bounded():
    from data.annotate import canonical_context_frame_indices

    normal = canonical_context_frame_indices(
        100,
        140,
        "00:00:10.000",
        "00:00:14.000",
        10.0,
        frames_per_shot=3,
        min_frame_interval_s=4.0,
        max_frames_per_shot=16,
    )
    short = canonical_context_frame_indices(
        100,
        101,
        "00:00:10.000",
        "00:00:10.100",
        10.0,
        frames_per_shot=3,
        min_frame_interval_s=4.0,
        max_frames_per_shot=16,
    )

    assert normal == [113, 127]
    assert short == [100]
    assert all(100 <= frame <= 140 for frame in normal)
    assert all(100 <= frame <= 101 for frame in short)


def test_packet_does_not_call_model_or_representative_frame_services(tmp_path, monkeypatch):
    _seed_project(tmp_path)
    _write_candidate(tmp_path)
    _patch_video(tmp_path, monkeypatch, context_indices=[])
    from services import frame_match, frame_retrieval, silhouette

    monkeypatch.setattr(
        silhouette,
        "load_sam_model",
        lambda *_args, **_kwargs: pytest.fail("SAM must not be loaded"),
    )
    monkeypatch.setattr(
        frame_match,
        "_load_clip_model",
        lambda *_args, **_kwargs: pytest.fail("CLIP must not be loaded"),
    )
    monkeypatch.setattr(
        frame_retrieval,
        "retrieve_single_frame",
        lambda *_args, **_kwargs: pytest.fail("representative frames must not be used"),
    )

    packet = reference.retrieve_silhouette_reference_packet(
        tmp_path, **_packet_kwargs(),
    )

    assert packet["metadata"]["provenance"]["new_inference_performed"] is False


def test_missing_video_and_payload_limit_are_structured_failures(tmp_path, monkeypatch):
    _seed_project(tmp_path)
    _write_candidate(tmp_path)
    video_path = tmp_path / "media" / "videos" / "movie" / _FILENAME
    video_path.unlink()
    _patch_video(tmp_path, monkeypatch, context_indices=[])

    with pytest.raises(reference.SilhouetteReferenceError) as missing_video:
        reference.retrieve_silhouette_reference_packet(tmp_path, **_packet_kwargs())
    assert missing_video.value.code == "SOURCE_VIDEO_MISSING"
    assert missing_video.value.stage == "FILM_RESOLUTION"

    video_path.write_bytes(b"placeholder video")
    monkeypatch.setattr(reference, "_MAX_PACKET_BYTES", 1)
    with pytest.raises(reference.SilhouetteReferenceError) as too_large:
        reference.retrieve_silhouette_reference_packet(tmp_path, **_packet_kwargs())
    assert too_large.value.code == "PAYLOAD_TOO_LARGE"
    assert too_large.value.stage == "PAYLOAD_SIZE_VALIDATION"
    assert too_large.value.details["content"][0]["content_id"] == "silhouette"