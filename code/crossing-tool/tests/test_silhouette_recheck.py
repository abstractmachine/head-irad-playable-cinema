"""Synthetic tests for bounded, source-annotation-driven silhouette rechecks."""

from __future__ import annotations

import json
import signal
from argparse import Namespace
from pathlib import Path

from PIL import Image
import pytest

import cli
from services.illustration_index import ALL, query_page, rebuild_index
from services.silhouette_catalog import (
    ASSIGNMENT_ACTIVE,
    ASSIGNMENT_INACTIVE,
    ASSIGNMENT_SUPERSEDED,
    RECHECK_ERROR,
    RECHECK_PENDING,
    extract_objects_for_shot,
)
from services.silhouette_curation import (
    mark_recheck_error,
    queue_recheck_pending,
    stage_recheck_replacements,
)


@pytest.fixture(autouse=True)
def _no_host_extractor(monkeypatch):
    from services.silhouette_recheck import _RecheckModels

    monkeypatch.setattr(
        "services.silhouette_recheck._uncoordinated_extraction_process_running",
        lambda: False,
    )
    monkeypatch.setattr(
        "services.silhouette_recheck._load_recheck_models",
        lambda *_args: _RecheckModels(object(), object(), "cpu", object(), "sam3.pt", "cpu"),
    )


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_old_questionable(
    project: Path,
    *,
    label: str = "ammunition belt",
    object_id: str = "object_0001",
    shot_id: str = "tmdb_1@f000010-f000020",
) -> tuple[Path, Path]:
    label_dir = project / "data" / "silhouettes" / "catalog" / "movie" / "film_a" / label.replace(" ", "_")
    label_dir.mkdir(parents=True)
    json_path = label_dir / f"{object_id}.json"
    png_path = label_dir / f"{object_id}.png"
    Image.new("RGBA", (20, 20), (120, 80, 30, 255)).save(png_path)
    json_path.write_text(json.dumps({
        "schema_version": "1",
        "media_type": "movie",
        "filename": "film_a.mp4",
        "filename_stem": "film_a",
        "media_id": "tmdb_1",
        "shot_id": shot_id,
        "frame": 12,
        "field": "objects",
        "label": label,
        "png": png_path.name,
        "source_frame": "frame:12",
        "assignment": {"state": ASSIGNMENT_ACTIVE},
        "search_provenance": {
            "state": "questionable",
            "method": "canonical_search",
            "historical_search_label": label,
        },
    }, indent=2) + "\n", encoding="utf-8")
    return json_path, png_path


def _write_source_annotation(
    project: Path,
    values: list[str],
    *,
    field: str = "objects",
    shot_id: str = "tmdb_1@f000010-f000020",
    append: bool = False,
) -> Path:
    path = project / "data" / "annotations" / "shots" / "movie" / "film_a.annotations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = json.loads(path.read_text(encoding="utf-8")) if append and path.exists() else []
    entries.append({
        "shot": {
            "shot_id": shot_id,
            "annotation": {field: values},
        },
    })
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _write_replacement(
    project: Path,
    label: str,
    *,
    object_id: str = "object_0001",
    shot_id: str = "tmdb_1@f000010-f000020",
) -> Path:
    replacement_dir = (
        project / "data" / "silhouettes" / "catalog" / "movie" / "film_a" / label.replace(" ", "_")
    )
    replacement_dir.mkdir(parents=True, exist_ok=True)
    replacement_png = replacement_dir / f"{object_id}.png"
    replacement_json = replacement_dir / f"{object_id}.json"
    Image.new("RGBA", (20, 20), (20, 130, 100, 255)).save(replacement_png)
    replacement_json.write_text(json.dumps({
        "schema_version": "1",
        "media_type": "movie",
        "filename": "film_a.mp4",
        "filename_stem": "film_a",
        "media_id": "tmdb_1",
        "shot_id": shot_id,
        "frame": 13,
        "field": "objects",
        "label": label,
        "png": replacement_png.name,
        "assignment": {"state": ASSIGNMENT_ACTIVE},
    }, indent=2) + "\n", encoding="utf-8")
    return replacement_png


def test_recheck_uses_source_annotation_not_historical_label(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, old_png = _write_old_questionable(tmp_path)
    source_annotation = _write_source_annotation(tmp_path, ["gun belt"])
    original_provenance = _load(old_json)["search_provenance"]
    original_png = old_png.read_bytes()
    source_before = source_annotation.read_bytes()
    calls: list[str] = []

    def fake_extract(*, label, **kwargs):
        calls.append(label)
        replacement_png = _write_replacement(tmp_path, label)
        return {"saved": [str(replacement_png)], "skipped": 0, "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", fake_extract)

    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)

    assert calls == ["gun belt"]
    assert report["completed"] == 1
    assert report["produced_objects"] == 1
    assert _load(old_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert _load(old_json)["assignment"]["recheck"]["annotation_values"] == ["gun belt"]
    assert _load(old_json)["search_provenance"] == original_provenance
    assert old_png.read_bytes() == original_png
    assert old_json.exists()
    assert source_annotation.read_bytes() == source_before
    replacement = _load(
        tmp_path / "data" / "silhouettes" / "catalog" / "movie" / "film_a" / "gun_belt" / "object_0001.json"
    )
    assert replacement["assignment"]["state"] == ASSIGNMENT_ACTIVE
    assert replacement["assignment"]["supersedes"]["object_id"] == "object_0001"


def test_recheck_groups_shared_source_records_into_one_job(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_paths = [
        _write_old_questionable(tmp_path, label=label)[0]
        for label in ("ammunition belt", "gun holster", "saddle bag")
    ]
    _write_source_annotation(tmp_path, ["revolver", "gun belt", "holster"])
    calls: list[str] = []

    def fake_extract(*, label, **kwargs):
        calls.append(label)
        return {"saved": [str(_write_replacement(tmp_path, label))], "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", fake_extract)

    report = run_silhouette_rechecks(
        tmp_path, media_type="movie", limit_sources=1,
    )

    assert report["source_jobs_examined"] == 1
    assert report["source_jobs_completed"] == 1
    assert report["historical_records_examined"] == 3
    assert report["historical_records_superseded"] == 3
    assert calls == ["revolver", "gun belt", "holster"]
    references = [_load(path)["assignment"]["superseded_by"] for path in old_paths]
    assert references[0] == references[1] == references[2]
    assert len(references[0]) == 3


def test_recheck_executes_each_of_two_source_jobs_once(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    first_shot = "tmdb_1@f000010-f000020"
    second_shot = "tmdb_1@f000030-f000040"
    for label in ("ammo belt", "gun holster"):
        _write_old_questionable(tmp_path, label=label, shot_id=first_shot)
    for label in ("saddle bag", "wooden post"):
        _write_old_questionable(tmp_path, label=label, shot_id=second_shot)
    _write_source_annotation(tmp_path, ["gun belt"], shot_id=first_shot)
    _write_source_annotation(tmp_path, ["wooden fence"], shot_id=second_shot, append=True)
    calls: list[tuple[str, str]] = []

    def fake_extract(*, label, shot_id, **kwargs):
        calls.append((shot_id, label))
        replacement = _write_replacement(
            tmp_path, label, object_id=f"object_{len(calls):04d}", shot_id=shot_id,
        )
        return {"saved": [str(replacement)], "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", fake_extract)
    report = run_silhouette_rechecks(
        tmp_path, media_type="movie", limit_sources=2,
    )

    assert report["source_jobs_examined"] == 2
    assert report["source_jobs_completed"] == 2
    assert report["historical_records_examined"] == 4
    assert report["historical_records_superseded"] == 4
    assert report["deduplication_ratio"] == 2.0
    assert calls == [(first_shot, "gun belt"), (second_shot, "wooden fence")]


def test_recheck_loads_models_once_and_reuses_them_for_every_extraction(tmp_path, monkeypatch):
    from services.silhouette_recheck import _RecheckModels, run_silhouette_rechecks

    first_shot = "tmdb_1@f000010-f000020"
    second_shot = "tmdb_1@f000030-f000040"
    _write_old_questionable(tmp_path, label="first old", shot_id=first_shot)
    _write_old_questionable(tmp_path, label="second old", shot_id=second_shot)
    _write_source_annotation(tmp_path, ["gun belt", "holster"], shot_id=first_shot)
    _write_source_annotation(tmp_path, ["wooden fence"], shot_id=second_shot, append=True)
    clip_model = object()
    clip_processor = object()
    mask_generator = object()
    loader_calls = []
    extraction_models = []

    def fake_load(project_path, sam_model_name, frame_model_name):
        loader_calls.append((project_path, sam_model_name, frame_model_name))
        return _RecheckModels(
            clip_model, clip_processor, "cuda", mask_generator, "sam3-effective", "cuda",
        )

    def fake_extract(*, label, shot_id, clip_model, clip_processor, clip_device,
                     mask_generator, sam_model_name, **kwargs):
        extraction_models.append((
            label, shot_id, clip_model, clip_processor, clip_device,
            mask_generator, sam_model_name,
        ))
        replacement = _write_replacement(
            tmp_path, label, object_id=f"object_{len(extraction_models):04d}", shot_id=shot_id,
        )
        return {"saved": [str(replacement)], "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck._load_recheck_models", fake_load)
    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", fake_extract)

    report = run_silhouette_rechecks(
        tmp_path, media_type="movie", limit_sources=2,
        sam_model_name="sam3-configured", frame_model_name="clip-configured",
    )

    assert report["source_jobs_completed"] == 2
    assert report["model_loads"] == 1
    assert loader_calls == [(str(tmp_path), "sam3-configured", "clip-configured")]
    assert [(label, shot_id) for label, shot_id, *_models in extraction_models] == [
        ("gun belt", first_shot), ("holster", first_shot), ("wooden fence", second_shot),
    ]
    assert all(models[2:] == (
        clip_model, clip_processor, "cuda", mask_generator, "sam3-effective",
    ) for models in extraction_models)


def test_recheck_record_limit_never_splits_a_source_job(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    for label in ("ammunition belt", "gun holster"):
        _write_old_questionable(tmp_path, label=label)
    _write_source_annotation(tmp_path, ["gun belt"])
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: pytest.fail("a source job must not be split to satisfy limit_records"),
    )

    report = run_silhouette_rechecks(
        tmp_path, media_type="movie", limit_records=1, dry_run=True,
    )

    assert report["source_jobs_available"] == 1
    assert report["historical_records_available"] == 2
    assert report["source_jobs_examined"] == 0
    assert report["historical_records_examined"] == 0


def test_recheck_reuses_completed_source_job_for_later_historical_record(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    first_old, _ = _write_old_questionable(tmp_path, label="ammunition belt")
    _write_source_annotation(tmp_path, ["gun belt"])
    calls: list[str] = []

    def first_extract(*, label, **kwargs):
        calls.append(label)
        return {"saved": [str(_write_replacement(tmp_path, label))], "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", first_extract)
    run_silhouette_rechecks(tmp_path, media_type="movie", limit_sources=1)

    later_old, _ = _write_old_questionable(tmp_path, label="gun holster")
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: pytest.fail("a completed source job must not re-extract"),
    )
    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit_sources=1)

    assert calls == ["gun belt"]
    assert report["source_jobs_completed"] == 1
    assert _load(first_old)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert _load(later_old)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert _load(first_old)["assignment"]["superseded_by"] == _load(later_old)["assignment"]["superseded_by"]


def test_pending_recheck_is_discovered_before_active_questionable_records(tmp_path):
    from services.silhouette_recheck import iter_recheck_candidates

    pending_json, _ = _write_old_questionable(tmp_path, label="pending object")
    active_json, _ = _write_old_questionable(tmp_path, label="active object")
    queue_recheck_pending(pending_json, annotation_values=["gun belt"])

    candidates = list(iter_recheck_candidates(tmp_path, media_type="movie"))

    assert [(kind, Path(record["path"]).name) for kind, record in candidates] == [
        ("pending", pending_json.name),
        ("questionable", active_json.name),
    ]


def test_recheck_preserves_atomic_source_values_and_multiple_replacements(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, _ = _write_old_questionable(tmp_path, label="saddle bag")
    _write_source_annotation(tmp_path, ["saddle", "bag"])
    calls: list[str] = []

    def fake_extract(*, label, **kwargs):
        calls.append(label)
        return {"saved": [str(_write_replacement(tmp_path, label))], "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", fake_extract)
    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)

    old = _load(old_json)
    assert calls == ["saddle", "bag"]
    assert old["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert old["assignment"]["recheck"]["annotation_values"] == ["saddle", "bag"]
    assert old["assignment"]["recheck"]["result"] == "objects"
    assert len(old["assignment"]["superseded_by"]) == 2
    assert report["produced_objects"] == 2


def test_recheck_zero_result_supersedes_historical_object(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, old_png = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    original_png = old_png.read_bytes()
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: {"saved": [], "reason": "SAM3 produced no masks"},
    )

    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)

    old = _load(old_json)
    assert report["completed"] == 1
    assert report["zero_results"] == 1
    assert old["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert old["assignment"]["recheck"]["state"] == "completed"
    assert old["assignment"]["recheck"]["result"] == "no_objects"
    assert old["assignment"]["superseded_by"] == []
    assert old_png.read_bytes() == original_png


def test_confidence_floor_rejection_completes_zero_result_source_job(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, old_png = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    original_png = old_png.read_bytes()
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: {
            "saved": [],
            "reason": "no masks above confidence floor 0.2 (best tight score: 0.184 for 1 candidates)",
        },
    )

    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit_sources=1)

    old = _load(old_json)
    assert report["source_jobs_completed"] == 1
    assert report["source_jobs_zero_result"] == 1
    assert report["source_jobs_failed"] == 0
    assert old["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert old["assignment"]["recheck"]["state"] == "completed"
    assert old["assignment"]["recheck"]["result"] == "no_objects"
    assert old_png.read_bytes() == original_png


def test_partial_success_then_confidence_floor_completes_source_job(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, _ = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt", "holster"])
    calls: list[str] = []

    def fake_extract(*, label, **kwargs):
        calls.append(label)
        if label == "holster":
            return {
                "saved": [],
                "reason": "no masks above confidence floor 0.2 (best tight score: 0.190 for 1 candidates)",
            }
        return {"saved": [str(_write_replacement(tmp_path, label))], "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", fake_extract)
    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit_sources=1)

    replacement = _load(
        tmp_path / "data" / "silhouettes" / "catalog" / "movie" / "film_a" / "gun_belt" / "object_0001.json"
    )
    assert calls == ["gun belt", "holster"]
    assert report["source_jobs_completed"] == 1
    assert report["source_jobs_zero_result"] == 0
    assert report["produced_objects"] == 1
    assert _load(old_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert replacement["assignment"]["state"] == ASSIGNMENT_ACTIVE
    assert replacement["assignment"].get("reason") is None
    assert replacement["assignment"].get("source_recheck_job") is None


def test_recheck_failure_leaves_old_object_unsuperseded(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, old_png = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    original_png = old_png.read_bytes()
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: {"saved": [], "reason": "CLIP load failed: unavailable"},
    )

    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)

    old = _load(old_json)
    assert report["failed"] == 1
    assert old["assignment"]["state"] == ASSIGNMENT_INACTIVE
    assert old["assignment"]["recheck"]["state"] == RECHECK_ERROR
    assert "CLIP load failed" in old["assignment"]["recheck"]["error"]
    assert old_png.read_bytes() == original_png


def test_source_job_failure_marks_every_linked_record_without_superseding(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_paths = [
        _write_old_questionable(tmp_path, label=label)[0]
        for label in ("ammunition belt", "gun holster")
    ]
    _write_source_annotation(tmp_path, ["gun belt"])
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: {"saved": [], "reason": "CLIP load failed: unavailable"},
    )

    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit_sources=1)

    assert report["source_jobs_failed"] == 1
    assert report["historical_records_failed"] == 2
    for old_path in old_paths:
        record = _load(old_path)
        assert record["assignment"]["state"] == ASSIGNMENT_INACTIVE
        assert record["assignment"]["recheck"]["state"] == RECHECK_ERROR
        assert "CLIP load failed" in record["assignment"]["recheck"]["error"]


def test_partial_source_job_failure_stages_success_until_retry_completes(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, _ = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt", "holster"])
    calls: list[str] = []

    def partial_extract(*, label, **kwargs):
        calls.append(label)
        if label == "holster":
            return {"saved": [], "reason": "CLIP load failed: unavailable"}
        return {"saved": [str(_write_replacement(tmp_path, label))], "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", partial_extract)
    first = run_silhouette_rechecks(tmp_path, media_type="movie", limit_sources=1)
    staged = _load(
        tmp_path / "data" / "silhouettes" / "catalog" / "movie" / "film_a" / "gun_belt" / "object_0001.json"
    )

    assert first["source_jobs_failed"] == 1
    assert _load(old_json)["assignment"]["state"] == ASSIGNMENT_INACTIVE
    assert staged["assignment"]["state"] == ASSIGNMENT_INACTIVE
    assert staged["assignment"]["reason"] == "source_recheck_staged"

    def retry_extract(*, label, **kwargs):
        calls.append(label)
        return {"saved": [str(_write_replacement(tmp_path, label))], "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", retry_extract)
    second = run_silhouette_rechecks(
        tmp_path, media_type="movie", limit_sources=1, retry_errors=True,
    )

    assert second["source_jobs_completed"] == 1
    assert calls == ["gun belt", "holster", "holster"]
    assert _load(old_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert _load(staged["path"] if "path" in staged else (
        tmp_path / "data" / "silhouettes" / "catalog" / "movie" / "film_a" / "gun_belt" / "object_0001.json"
    ))["assignment"]["state"] == ASSIGNMENT_ACTIVE


def test_confidence_floor_retry_reuses_staged_replacement_without_duplication(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, _ = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt", "holster"])
    staged_png = _write_replacement(tmp_path, "gun belt")
    source_job = {
        "media_type": "movie",
        "media_id": "tmdb_1",
        "shot_id": "tmdb_1@f000010-f000020",
        "field": "objects",
    }
    stage_recheck_replacements(
        source_job=source_job,
        replacement_json_paths=[staged_png.with_suffix(".json")],
    )
    queue_recheck_pending(old_json, annotation_values=["gun belt", "holster"])
    mark_recheck_error(
        old_json,
        error="no masks above confidence floor 0.2 (best tight score: 0.184 for 1 candidates)",
    )
    extraction_calls: list[str] = []
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda *, label, **kwargs: extraction_calls.append(label) or {
            "saved": [],
            "reason": "no masks above confidence floor 0.2 (best tight score: 0.184 for 1 candidates)",
        },
    )
    catalog_count_before = len(list((tmp_path / "data" / "silhouettes" / "catalog").rglob("object_*.json")))

    report = run_silhouette_rechecks(
        tmp_path, media_type="movie", limit_sources=1, retry_errors=True,
    )

    assert extraction_calls == ["holster"]
    assert report["source_jobs_completed"] == 1
    assert report["produced_objects"] == 1
    assert _load(old_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert _load(staged_png.with_suffix(".json"))["assignment"]["state"] == ASSIGNMENT_ACTIVE
    assert len(list((tmp_path / "data" / "silhouettes" / "catalog").rglob("object_*.json"))) == catalog_count_before


def test_retrying_seven_failed_source_jobs_reuses_outputs_without_duplicates(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    affected_counts = (1, 20, 16, 79, 2, 2, 6)
    historical_paths: list[Path] = []
    source_values: list[str] = []
    for job_index, record_count in enumerate(affected_counts):
        shot_id = f"tmdb_1@f{job_index + 1:06d}-f{job_index + 2:06d}"
        value = f"current value {job_index}"
        source_values.append(value)
        _write_source_annotation(
            tmp_path, [value], shot_id=shot_id, append=job_index > 0,
        )
        for record_index in range(record_count):
            path, _png = _write_old_questionable(
                tmp_path,
                label=f"historical label {job_index}-{record_index}",
                shot_id=shot_id,
            )
            queue_recheck_pending(path, annotation_values=[value])
            mark_recheck_error(
                path,
                error="no masks above confidence floor 0.2 (best tight score: 0.190 for 1 candidates)",
            )
            historical_paths.append(path)

    staged_png = _write_replacement(
        tmp_path, source_values[0], object_id="object_9001", shot_id="tmdb_1@f000001-f000002",
    )
    stage_recheck_replacements(
        source_job={
            "media_type": "movie", "media_id": "tmdb_1",
            "shot_id": "tmdb_1@f000001-f000002", "field": "objects",
        },
        replacement_json_paths=[staged_png.with_suffix(".json")],
    )
    active_png = _write_replacement(
        tmp_path, source_values[1], object_id="object_9002", shot_id="tmdb_1@f000002-f000003",
    )
    extraction_calls: list[str] = []
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda *, label, **kwargs: extraction_calls.append(label) or {
            "saved": [],
            "reason": "no masks above confidence floor 0.2 (best tight score: 0.190 for 1 candidates)",
        },
    )
    catalog_count_before = len(list((tmp_path / "data" / "silhouettes" / "catalog").rglob("object_*.json")))

    first = run_silhouette_rechecks(
        tmp_path, media_type="movie", limit_sources=7, retry_errors=True,
    )
    second = run_silhouette_rechecks(
        tmp_path, media_type="movie", limit_sources=7, retry_errors=True,
    )

    assert first["source_jobs_completed"] == 7
    assert first["source_jobs_zero_result"] == 5
    assert first["historical_records_superseded"] == 126
    assert first["produced_objects"] == 2
    assert first["source_jobs_failed"] == 0
    assert extraction_calls == source_values[2:]
    assert second["source_jobs_examined"] == 0
    assert all(_load(path)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED for path in historical_paths)
    assert _load(staged_png.with_suffix(".json"))["assignment"]["state"] == ASSIGNMENT_ACTIVE
    assert _load(active_png.with_suffix(".json"))["assignment"]["state"] == ASSIGNMENT_ACTIVE
    assert len(list((tmp_path / "data" / "silhouettes" / "catalog").rglob("object_*.json"))) == catalog_count_before


def test_recheck_error_can_be_retried_with_current_source_values(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, _ = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: {"saved": [], "reason": "CLIP load failed: unavailable"},
    )
    run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)

    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda *, label, **kwargs: {"saved": [str(_write_replacement(tmp_path, label))], "reason": "ok"},
    )
    report = run_silhouette_rechecks(
        tmp_path, media_type="movie", limit=1, retry_errors=True,
    )

    assert report["completed"] == 1
    assert _load(old_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED


def test_recheck_is_idempotent_after_completion(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, _ = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    calls: list[str] = []

    def fake_extract(*, label, **kwargs):
        calls.append(label)
        return {"saved": [str(_write_replacement(tmp_path, label))], "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", fake_extract)
    first = run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)
    second = run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)

    assert first["completed"] == 1
    assert second["examined"] == 0
    assert calls == ["gun belt"]
    assert _load(old_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED


def test_recheck_dry_run_and_limit_do_not_mutate_catalog(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    first_json, _ = _write_old_questionable(tmp_path, label="ammunition belt")
    second_json, _ = _write_old_questionable(tmp_path, label="saddle bag")
    _write_source_annotation(tmp_path, ["gun belt"])
    before = {path: path.read_bytes() for path in (first_json, second_json)}
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: pytest.fail("dry-run must not extract"),
    )

    report = run_silhouette_rechecks(
        tmp_path, media_type="movie", limit_sources=1, dry_run=True,
    )

    assert report["source_jobs_examined"] == 1
    assert report["historical_records_examined"] == 2
    assert report["eligible"] == 2
    assert report["attempted"] == 0
    assert report["completed"] == 0
    assert report["outcomes"][0]["recheck_values"] == ["gun belt"]
    assert {path: path.read_bytes() for path in before} == before


def test_recheck_marks_missing_source_or_field_as_explicit_error(tmp_path):
    from services.silhouette_recheck import run_silhouette_rechecks

    missing_source_json, _ = _write_old_questionable(tmp_path, label="missing source")
    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)
    assert report["failed"] == 1
    assert _load(missing_source_json)["assignment"]["recheck"]["state"] == RECHECK_ERROR

    field_json, _ = _write_old_questionable(tmp_path, label="missing field")
    _write_source_annotation(tmp_path, ["tree"], field="animals")
    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)
    assert report["failed"] == 1
    assert "no 'objects' annotation" in _load(field_json)["assignment"]["recheck"]["error"]


def test_recheck_reuses_existing_active_replacement_without_duplicate_extraction(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, _ = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    replacement_png = _write_replacement(tmp_path, "gun belt")
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: pytest.fail("existing active replacement must prevent extraction"),
    )

    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)

    assert report["completed"] == 1
    assert report["produced_objects"] == 1
    assert _load(old_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert _load(replacement_png.with_suffix(".json"))["assignment"]["state"] == ASSIGNMENT_ACTIVE
    rebuild_index(tmp_path, "silhouettes", "movie")
    active = query_page(tmp_path, "silhouettes", "movie", limit=20)["records"]
    historical = query_page(tmp_path, "silhouettes", "movie", assignment_state=ALL, limit=20)["records"]
    assert [record["label"] for record in active] == ["gun belt"]
    assert {record["label"] for record in historical} == {"ammunition belt", "gun belt"}


def test_shared_replacement_preserves_relationships_to_each_old_object(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    first_old, _ = _write_old_questionable(tmp_path, label="ammunition belt")
    second_old, _ = _write_old_questionable(tmp_path, label="gun holster")
    _write_source_annotation(tmp_path, ["gun belt"])
    replacement_png = _write_replacement(tmp_path, "gun belt")
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: pytest.fail("existing active replacement must prevent extraction"),
    )

    report = run_silhouette_rechecks(tmp_path, media_type="movie", limit=2)

    replacement = _load(replacement_png.with_suffix(".json"))
    assert report["completed"] == 2
    assert _load(first_old)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert _load(second_old)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert replacement["assignment"]["supersedes"] == [
        {"media_type": "movie", "filename_stem": "film_a", "label": "ammunition belt", "object_id": "object_0001"},
        {"media_type": "movie", "filename_stem": "film_a", "label": "gun holster", "object_id": "object_0001"},
    ]


def test_automated_pending_recheck_bypasses_only_queued_atomic_value(tmp_path):
    old_json, _ = _write_old_questionable(tmp_path)
    queue_recheck_pending(old_json, annotation_values=["gun belt"])

    allowed = extract_objects_for_shot(
        project_path=str(tmp_path), label="gun belt", field="objects",
        shot_id="tmdb_1@f000010-f000020", filename="film_a.mp4", media_id="tmdb_1",
        media_type="movie", sam_model_name="sam3", frame_model_name="clip",
        recheck_source_json_path=old_json,
    )
    rejected = extract_objects_for_shot(
        project_path=str(tmp_path), label="ammunition belt", field="objects",
        shot_id="tmdb_1@f000010-f000020", filename="film_a.mp4", media_id="tmdb_1",
        media_type="movie", sam_model_name="sam3", frame_model_name="clip",
        recheck_source_json_path=old_json,
    )

    assert allowed["reason"].startswith("video not found:")
    assert rejected["reason"] == "recheck request is not pending for this extraction identity"
    assert _load(old_json)["assignment"]["recheck"]["state"] == RECHECK_PENDING


def test_cli_recheck_dry_run_dispatches_bounded_options(monkeypatch, tmp_path, capsys):
    captured = {}
    report = {
        "command": {"dry_run": True},
        "source_jobs_available": 4,
        "source_jobs_examined": 1,
        "source_jobs_attempted": 0,
        "source_jobs_completed": 0,
        "source_jobs_zero_result": 0,
        "source_jobs_failed": 0,
        "historical_records_available": 7,
        "historical_records_examined": 2,
        "historical_records_superseded": 0,
        "historical_records_skipped": 0,
        "historical_records_failed": 0,
        "duplicate_source_jobs_avoided": 1,
        "deduplication_ratio": 1.75,
        "errors": 0,
        "produced_objects": 0,
        "outcomes": [{
            "outcome": "eligible", "source_job": "movie/tmdb_1/shot/objects",
            "historical_record_count": 2, "recheck_values": ["gun belt"], "error": "",
        }],
        "index_results": {},
        "artifacts": {"report_json": "report.json", "outcomes_csv": "outcomes.csv"},
    }

    monkeypatch.setattr(
        cli.prefs,
        "get",
        lambda key, default=None: str(tmp_path) if key == "path" else default,
    )
    monkeypatch.setattr(
        "services.silhouette_recheck.run_silhouette_rechecks",
        lambda *args, **kwargs: captured.setdefault("kwargs", kwargs) and report,
    )
    args = Namespace(
        silhouette_action="recheck", media="gameplay", field="objects",
        limit_records=None, limit_sources=10,
        dry_run=True, retry_errors=False, model="sam-test", frame_model="clip-test", verbose=False,
    )

    cli._index_silhouette(args)

    assert captured["kwargs"] == {
        "media_type": "gameplay", "field": "objects", "limit_records": None,
        "limit_sources": 10,
        "all_sources": False,
        "dry_run": True, "retry_errors": False, "sam_model_name": "sam-test",
        "frame_model_name": "clip-test", "verbose": False, "log_path": None,
    }
    assert "Silhouette recheck (dry-run)" in capsys.readouterr().out


def test_cli_recheck_requires_exactly_one_processing_mode():
    parser = cli.build_parser()
    base = ["index", "silhouette", "recheck", "--media", "movie", "--field", "objects"]

    all_args = parser.parse_args([*base, "--all"])
    source_args = parser.parse_args([*base, "--limit-sources", "10"])
    record_args = parser.parse_args([*base, "--limit-records", "100"])

    assert all_args.all_sources is True
    assert source_args.limit_sources == 10
    assert record_args.limit_records == 100
    with pytest.raises(SystemExit):
        parser.parse_args(base)
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--all", "--limit-sources", "10"])
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--all", "--limit-records", "100"])
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--limit-sources", "10", "--limit-records", "100"])


def test_all_dry_run_enumerates_every_source_job_without_models_or_mutation(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    first_json, _ = _write_old_questionable(tmp_path, label="first", shot_id="tmdb_1@f000010-f000020")
    second_json, _ = _write_old_questionable(tmp_path, label="second", shot_id="tmdb_1@f000030-f000040")
    _write_source_annotation(tmp_path, ["gun belt"], shot_id="tmdb_1@f000010-f000020")
    _write_source_annotation(tmp_path, ["holster"], shot_id="tmdb_1@f000030-f000040", append=True)
    before = {path: path.read_bytes() for path in (first_json, second_json)}
    monkeypatch.setattr(
        "services.silhouette_recheck._load_recheck_models",
        lambda *_args: pytest.fail("dry-run must not load models"),
    )
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: pytest.fail("dry-run must not extract"),
    )

    report = run_silhouette_rechecks(
        tmp_path, media_type="movie", all_sources=True, dry_run=True,
    )

    assert report["command"]["mode"] == "all"
    assert report["command"]["all"] is True
    assert report["source_jobs_available"] == 2
    assert report["source_jobs_examined"] == 2
    assert report["historical_records_examined"] == 2
    assert report["model_loads"] == 0
    assert report["index_results"] == {}
    assert {path: path.read_bytes() for path in before} == before


def test_all_run_survives_interruption_without_completing_current_job(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    first_json, _ = _write_old_questionable(tmp_path, label="first", shot_id="tmdb_1@f000010-f000020")
    second_json, _ = _write_old_questionable(tmp_path, label="second", shot_id="tmdb_1@f000030-f000040")
    _write_source_annotation(tmp_path, ["gun belt"], shot_id="tmdb_1@f000010-f000020")
    _write_source_annotation(tmp_path, ["holster"], shot_id="tmdb_1@f000030-f000040", append=True)

    def fake_extract(*, label, **kwargs):
        if label == "holster":
            raise KeyboardInterrupt
        return {"saved": [str(_write_replacement(tmp_path, label))], "reason": "ok"}

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", fake_extract)
    report = run_silhouette_rechecks(tmp_path, media_type="movie", all_sources=True)

    assert report["interrupted"] is True
    assert report["source_jobs_completed"] == 1
    assert _load(first_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert _load(second_json)["assignment"]["state"] == ASSIGNMENT_INACTIVE
    assert _load(second_json)["assignment"]["recheck"]["state"] == RECHECK_PENDING
    assert report["index_results"] == {}


def test_sigint_after_source_commit_preserves_completed_job_then_stops(tmp_path, monkeypatch):
    from services import silhouette_curation
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, _ = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda *, label, **kwargs: {
            "saved": [str(_write_replacement(tmp_path, label))], "reason": "ok",
        },
    )
    original_persist = silhouette_curation._persist_record
    sent = False

    def interrupt_after_replacement(path, record):
        nonlocal sent
        original_persist(path, record)
        if not sent and record.get("assignment", {}).get("state") == ASSIGNMENT_ACTIVE:
            sent = True
            signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(silhouette_curation, "_persist_record", interrupt_after_replacement)
    report = run_silhouette_rechecks(tmp_path, media_type="movie", all_sources=True)

    replacement_json = (
        tmp_path / "data" / "silhouettes" / "catalog" / "movie" / "film_a"
        / "gun_belt" / "object_0001.json"
    )
    assert sent is True
    assert report["interrupted"] is True
    assert report["source_jobs_completed"] == 1
    assert report["outcomes"][0]["outcome"] == "completed"
    assert report["index_results"] == {}
    assert _load(old_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert _load(replacement_json)["assignment"]["state"] == ASSIGNMENT_ACTIVE


def test_recheck_log_appends_flushes_and_works_without_verbose(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: {"saved": [], "reason": "no masks above confidence floor 0.2"},
    )
    log_path = tmp_path / "logs" / "recheck.log"
    log_path.parent.mkdir()
    log_path.write_text("prior run\n", encoding="utf-8")

    report = run_silhouette_rechecks(
        tmp_path, media_type="movie", all_sources=True, log_path=log_path,
    )

    log = log_path.read_text(encoding="utf-8")
    assert report["command"]["log_path"] == str(log_path)
    assert Path(report["artifacts"]["report_json"]).is_file()
    assert Path(report["artifacts"]["outcomes_csv"]).is_file()
    persisted = _load(Path(report["artifacts"]["report_json"]))
    assert persisted["artifacts"] == report["artifacts"]
    assert log.startswith("prior run\n")
    assert "SILHOUETTE RECHECK RUN" in log
    assert "mode=all" in log
    assert "outcome=zero_results" in log
    assert "finished_at=" in log


def test_verbose_recheck_also_writes_requested_log(tmp_path, monkeypatch, capsys):
    from services.silhouette_recheck import run_silhouette_rechecks

    _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: {"saved": [], "reason": "no masks above confidence floor 0.2"},
    )
    log_path = tmp_path / "recheck.log"

    run_silhouette_rechecks(
        tmp_path, media_type="movie", all_sources=True, verbose=True, log_path=log_path,
    )

    assert "outcome=zero_results" in capsys.readouterr().out
    assert "outcome=zero_results" in log_path.read_text(encoding="utf-8")


def test_all_run_skips_completed_jobs_and_continues_after_ordinary_failure(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    completed_json, _ = _write_old_questionable(
        tmp_path, label="completed", shot_id="tmdb_1@f000010-f000020",
    )
    failing_json, _ = _write_old_questionable(
        tmp_path, label="failing", shot_id="tmdb_1@f000030-f000040",
    )
    succeeding_json, _ = _write_old_questionable(
        tmp_path, label="succeeding", shot_id="tmdb_1@f000050-f000060",
    )
    _write_source_annotation(tmp_path, ["complete value"], shot_id="tmdb_1@f000010-f000020")
    _write_source_annotation(tmp_path, ["fail value"], shot_id="tmdb_1@f000030-f000040", append=True)
    _write_source_annotation(tmp_path, ["success value"], shot_id="tmdb_1@f000050-f000060", append=True)
    completed_png = _write_replacement(
        tmp_path, "complete value", object_id="object_7001", shot_id="tmdb_1@f000010-f000020",
    )
    queue_recheck_pending(completed_json, annotation_values=["complete value"])
    from services.silhouette_curation import complete_recheck_source
    complete_recheck_source(
        tmp_path,
        old_json_paths=[completed_json],
        replacement_json_paths=[completed_png.with_suffix(".json")],
        annotation_values=["complete value"],
    )
    calls: list[str] = []

    def fake_extract(*, label, shot_id, **kwargs):
        calls.append(label)
        if label == "fail value":
            return {"saved": [], "reason": "frame selection failed: transient fixture failure"}
        return {
            "saved": [str(_write_replacement(tmp_path, label, shot_id=shot_id))],
            "reason": "ok",
        }

    monkeypatch.setattr("services.silhouette_recheck.extract_objects_for_shot", fake_extract)
    report = run_silhouette_rechecks(tmp_path, media_type="movie", all_sources=True)

    assert report["source_jobs_examined"] == 2
    assert report["source_jobs_completed"] == 1
    assert report["source_jobs_failed"] == 1
    assert calls == ["fail value", "success value"]
    assert _load(completed_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED
    assert _load(failing_json)["assignment"]["recheck"]["state"] == RECHECK_ERROR
    assert _load(succeeding_json)["assignment"]["state"] == ASSIGNMENT_SUPERSEDED


def test_verbose_recheck_writes_terminal_progress_without_log(tmp_path, monkeypatch, capsys):
    from services.silhouette_recheck import run_silhouette_rechecks

    _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    monkeypatch.setattr(
        "services.silhouette_recheck.extract_objects_for_shot",
        lambda **kwargs: {"saved": [], "reason": "no masks above confidence floor 0.2"},
    )

    run_silhouette_rechecks(tmp_path, media_type="movie", all_sources=True, verbose=True)

    output = capsys.readouterr().out
    assert "queue source_jobs_available=1" in output
    assert "outcome=zero_results" in output


def test_invalid_log_path_fails_before_catalog_mutation(tmp_path):
    from services.silhouette_recheck import run_silhouette_rechecks

    old_json, _ = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    before = old_json.read_bytes()

    with pytest.raises(ValueError, match="Could not open recheck log"):
        run_silhouette_rechecks(
            tmp_path, media_type="movie", all_sources=True,
            log_path=blocked_parent / "recheck.log",
        )

    assert old_json.read_bytes() == before


def test_log_write_failure_stops_run_without_marking_current_job_failed(tmp_path, monkeypatch):
    from services.silhouette_recheck import RecheckLogError, run_silhouette_rechecks

    old_json, _ = _write_old_questionable(tmp_path)
    _write_source_annotation(tmp_path, ["gun belt"])
    original_emit = __import__("services.silhouette_recheck", fromlist=["_emit"])._emit

    def fail_model_log(logger, verbose, message):
        if message.startswith("models "):
            raise RecheckLogError("log device unavailable")
        return original_emit(logger, verbose, message)

    monkeypatch.setattr("services.silhouette_recheck._emit", fail_model_log)
    report = run_silhouette_rechecks(tmp_path, media_type="movie", all_sources=True)

    assert report["fatal_error"] == "log device unavailable"
    assert report["source_jobs_failed"] == 0
    assert report["index_results"] == {}
    assert _load(old_json)["assignment"]["state"] == ASSIGNMENT_INACTIVE
    assert _load(old_json)["assignment"]["recheck"]["state"] == RECHECK_PENDING


def test_recheck_refuses_an_uncoordinated_live_extractor(tmp_path, monkeypatch):
    from services.silhouette_recheck import run_silhouette_rechecks

    monkeypatch.setattr(
        "services.silhouette_recheck._uncoordinated_extraction_process_running",
        lambda: True,
    )

    with pytest.raises(RuntimeError, match="extraction process is already active"):
        run_silhouette_rechecks(tmp_path, media_type="movie", limit=1)