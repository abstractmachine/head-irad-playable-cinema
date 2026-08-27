"""Synthetic lifecycle tests for non-destructive silhouette assignment state."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from services.illustration_index import ALL, query_page, rebuild_index
from services.silhouette_catalog import (
    ASSIGNMENT_ACTIVE,
    ASSIGNMENT_INACTIVE,
    ASSIGNMENT_SUPERSEDED,
    RECHECK_COMPLETED,
    RECHECK_NO_RESULT,
    assignment_is_active,
    assignment_state_for_record,
    scan_catalog,
    extract_objects_for_shot,
)
from services.silhouette_curation import (
    CURATORIAL_REJECTION,
    complete_recheck,
    get_pending_rechecks,
    mark_recheck_no_result,
    mark_recheck_pending,
)


def _write_catalog_object(
    project: Path,
    *,
    object_index: int,
    assignment: dict | None = None,
    media_type: str = "movie",
    media_id: str = "tmdb_1",
    shot_id: str = "tmdb_1@f000001-f000010",
    field: str = "wearing",
    label: str = "arm band",
) -> tuple[Path, Path]:
    label_dir = (
        project / "data" / "silhouettes" / "catalog" / media_type
        / "film_a" / label.replace(" ", "_")
    )
    label_dir.mkdir(parents=True, exist_ok=True)
    json_path = label_dir / f"object_{object_index:04d}.json"
    png_path = label_dir / f"object_{object_index:04d}.png"
    Image.new("RGBA", (32, 24), (20 * object_index, 120, 180, 255)).save(png_path)
    record = {
        "schema_version": "1",
        "media_type": media_type,
        "filename": "film_a.mp4",
        "filename_stem": "film_a",
        "media_id": media_id,
        "shot_id": shot_id,
        "frame": 5,
        "field": field,
        "label": label,
        "png": png_path.name,
        "source_frame": "frame:5",
        "search_provenance": {
            "state": "valid",
            "match": "number_variant",
            "preference": "non_preferred",
            "annotation_value": "arm bands",
        },
    }
    if assignment is not None:
        record["assignment"] = assignment
    json_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return json_path, png_path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_legacy_and_explicit_assignments_filter_normal_illustration_browse(tmp_path):
    legacy_path, _ = _write_catalog_object(tmp_path, object_index=1)
    active_path, _ = _write_catalog_object(
        tmp_path,
        object_index=2,
        assignment={"state": ASSIGNMENT_ACTIVE},
        shot_id="tmdb_1@f000011-f000020",
    )
    inactive_path, _ = _write_catalog_object(
        tmp_path,
        object_index=3,
        assignment={"state": ASSIGNMENT_INACTIVE, "reason": CURATORIAL_REJECTION},
        shot_id="tmdb_1@f000021-f000030",
    )
    superseded_path, _ = _write_catalog_object(
        tmp_path,
        object_index=4,
        assignment={"state": ASSIGNMENT_SUPERSEDED, "superseded_by": {"object_id": "object_0002"}},
        shot_id="tmdb_1@f000031-f000040",
    )

    assert assignment_state_for_record(_load(legacy_path)) == ASSIGNMENT_ACTIVE
    assert assignment_state_for_record(_load(active_path)) == ASSIGNMENT_ACTIVE
    assert assignment_state_for_record(_load(inactive_path)) == ASSIGNMENT_INACTIVE
    assert assignment_state_for_record(_load(superseded_path)) == ASSIGNMENT_SUPERSEDED

    rebuild_index(tmp_path, "silhouettes", "movie")
    normal = query_page(tmp_path, "silhouettes", "movie", limit=20)
    historical = query_page(
        tmp_path,
        "silhouettes",
        "movie",
        assignment_state=ALL,
        limit=20,
    )

    assert {Path(record["path"]).name for record in normal["records"]} == {
        legacy_path.name,
        active_path.name,
    }
    assert {Path(record["path"]).name for record in historical["records"]} == {
        legacy_path.name,
        active_path.name,
        inactive_path.name,
        superseded_path.name,
    }


def test_deassign_embeds_bounded_recheck_without_deleting_assets(tmp_path):
    old_path, old_png = _write_catalog_object(tmp_path, object_index=1)
    original_provenance = _load(old_path)["search_provenance"]

    updated = mark_recheck_pending(old_path, annotation_value="arm bands")
    assignment = updated["assignment"]
    recheck = assignment["recheck"]

    assert assignment["state"] == ASSIGNMENT_INACTIVE
    assert assignment["reason"] == CURATORIAL_REJECTION
    assert recheck["state"] == "pending"
    assert recheck["annotation_value"] == "arm bands"
    assert recheck["search_label"] == "arm band"
    assert recheck["media_type"] == "movie"
    assert recheck["media_id"] == "tmdb_1"
    assert recheck["shot_id"] == "tmdb_1@f000001-f000010"
    assert recheck["field"] == "wearing"
    assert old_path.exists()
    assert old_png.exists()
    assert _load(old_path)["search_provenance"] == original_provenance

    pending = get_pending_rechecks(tmp_path)
    assert len(pending) == 1
    assert pending[0]["annotation_value"] == "arm bands"
    assert pending[0]["identity"]["search_label"] == "arm band"

    no_result = mark_recheck_no_result(old_path)
    assert no_result["assignment"]["state"] == ASSIGNMENT_INACTIVE
    assert no_result["assignment"]["recheck"]["state"] == RECHECK_NO_RESULT
    assert old_path.exists()
    assert old_png.exists()
    assert get_pending_rechecks(tmp_path) == []


def test_pending_recheck_bypasses_only_matching_per_shot_cache(tmp_path):
    old_path, _ = _write_catalog_object(tmp_path, object_index=1)
    mark_recheck_pending(old_path, annotation_value="arm bands")

    cached = extract_objects_for_shot(
        project_path=str(tmp_path),
        label="arm band",
        field="wearing",
        shot_id="tmdb_1@f000001-f000010",
        filename="film_a.mp4",
        media_id="tmdb_1",
        media_type="movie",
        sam_model_name="sam3",
        frame_model_name="clip",
    )
    assert cached["reason"] == "cached"

    bypassed = extract_objects_for_shot(
        project_path=str(tmp_path),
        label="arm band",
        field="wearing",
        shot_id="tmdb_1@f000001-f000010",
        filename="film_a.mp4",
        media_id="tmdb_1",
        media_type="movie",
        sam_model_name="sam3",
        frame_model_name="clip",
        recheck_source_json_path=old_path,
    )
    assert bypassed["reason"].startswith("video not found:")

    mismatch = extract_objects_for_shot(
        project_path=str(tmp_path),
        label="arm bands",
        field="wearing",
        shot_id="tmdb_1@f000001-f000010",
        filename="film_a.mp4",
        media_id="tmdb_1",
        media_type="movie",
        sam_model_name="sam3",
        frame_model_name="clip",
        recheck_source_json_path=old_path,
    )
    assert mismatch["reason"] == "recheck request is not pending for this extraction identity"


def test_fresh_catalog_extraction_writes_explicit_active_assignment(tmp_path, monkeypatch):
    video_path = tmp_path / "media" / "videos" / "movie" / "film_a.mp4"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"placeholder")
    image = Image.new("RGB", (100, 100), (160, 120, 80))
    segmentation = np.zeros((100, 100), dtype=bool)
    segmentation[20:60, 20:60] = True
    mask = {"segmentation": segmentation, "area": 1600, "bbox": [20, 20, 40, 40]}

    class _MaskGenerator:
        def segment_concept(self, _image, _label):
            return [mask]

    monkeypatch.setattr("services.frame_match._get_video_fps", lambda _path: 24.0)
    monkeypatch.setattr(
        "services.frame_match._load_clip_model",
        lambda *args, **kwargs: (object(), object(), "cpu"),
    )
    monkeypatch.setattr(
        "services.frame_match.find_query_best_frame_for_shot",
        lambda **kwargs: (5, 0.9),
    )
    monkeypatch.setattr(
        "services.silhouette.extract_frame_as_pil",
        lambda *args, **kwargs: image,
    )
    monkeypatch.setattr(
        "services.silhouette.rank_masks_by_clip",
        lambda *args, **kwargs: [(mask, 0.9)],
    )
    monkeypatch.setattr(
        "services.silhouette.rerank_by_tight_crop",
        lambda *args, **kwargs: [(mask, 0.9, 0.9)],
    )
    monkeypatch.setattr(
        "services.silhouette_semantics.analyze_silhouette_semantics",
        lambda *args, **kwargs: {},
    )

    result = extract_objects_for_shot(
        project_path=str(tmp_path),
        label="arm band",
        field="wearing",
        shot_id="tmdb_1@f000001-f000010",
        filename="film_a.mp4",
        media_id="tmdb_1",
        media_type="movie",
        sam_model_name="sam3",
        frame_model_name="clip",
        mask_generator=_MaskGenerator(),
    )

    assert len(result["saved"]) == 1
    metadata = _load(Path(result["saved"][0]).with_suffix(".json"))
    assert metadata["assignment"] == {"state": ASSIGNMENT_ACTIVE}


def test_successful_recheck_supersedes_prior_active_duplicates_without_deletion(tmp_path):
    old_path, old_png = _write_catalog_object(tmp_path, object_index=1)
    duplicate_path, duplicate_png = _write_catalog_object(tmp_path, object_index=2)
    new_path, new_png = _write_catalog_object(
        tmp_path,
        object_index=3,
        assignment={"state": ASSIGNMENT_ACTIVE},
    )
    mark_recheck_pending(old_path, annotation_value="arm bands")

    old, new = complete_recheck(
        tmp_path,
        old_json_path=old_path,
        new_json_path=new_path,
    )
    old_on_disk = _load(old_path)
    duplicate_on_disk = _load(duplicate_path)
    new_on_disk = _load(new_path)

    assert old["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert old_on_disk["assignment"]["recheck"]["state"] == RECHECK_COMPLETED
    assert old_on_disk["assignment"]["superseded_by"]["object_id"] == "object_0003"
    assert new["assignment"]["state"] == ASSIGNMENT_ACTIVE
    assert new_on_disk["assignment"]["supersedes"]["object_id"] == "object_0001"
    assert duplicate_on_disk["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert duplicate_on_disk["assignment"]["superseded_by"]["object_id"] == "object_0003"
    assert old_png.exists()
    assert duplicate_png.exists()
    assert new_png.exists()
    assert old_on_disk["search_provenance"]["state"] == "valid"

    records = scan_catalog(str(tmp_path), media_type="movie")
    active = [record for record in records if assignment_is_active(record)]
    assert [Path(record["path"]).name for record in active] == [new_path.name]
