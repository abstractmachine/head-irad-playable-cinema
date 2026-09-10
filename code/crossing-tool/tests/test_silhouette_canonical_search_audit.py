from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

import cli
from data.media_id import build_shot_id
from data.metadata import save_json_metadata
from data.shotlist import write_shotlist
import services.search as search_mod
from services import silhouette_canonical_search_audit as audit_module
from services.silhouette_canonical_search_audit import (
    QUESTIONABLE,
    UNVERIFIABLE,
    VALID,
    audit_silhouette_canonical_search,
    default_output_dir,
)


def _write_png(path: Path, color: tuple[int, int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (80, 48), color).save(path)


def _write_catalog_object(
    project_path: Path,
    *,
    filename: str,
    media_id: str,
    shot_id: str,
    frame: int,
    label: str,
    field: str,
    object_index: int,
    source_frame: Path,
) -> Path:
    stem = Path(filename).stem
    label_dir = project_path / "data" / "silhouettes" / "catalog" / "movie" / stem / label.replace(" ", "_")
    label_dir.mkdir(parents=True, exist_ok=True)
    json_path = label_dir / f"object_{object_index:04d}.json"
    png_path = json_path.with_suffix(".png")
    _write_png(png_path, (80, 130, 190, 255))
    json_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "media_type": "movie",
                "filename": filename,
                "filename_stem": stem,
                "media_id": media_id,
                "shot_id": shot_id,
                "frame": frame,
                "label": label,
                "field": field,
                "source_frame": str(source_frame),
                "png": png_path.name,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return json_path


def _make_project(project_path: Path) -> tuple[str, str, dict[str, str], Path]:
    filename = "Film One.mp4"
    media_id = "file_film_one"
    shot_ids = {
        "coat_one": build_shot_id(media_id, 0, 100),
        "coat_two": build_shot_id(media_id, 101, 200),
        "yellow_exact": build_shot_id(media_id, 201, 300),
        "yellow_split": build_shot_id(media_id, 301, 400),
        "independent_words": build_shot_id(media_id, 401, 500),
        "plural": build_shot_id(media_id, 501, 600),
        "zero_results": build_shot_id(media_id, 601, 700),
    }
    save_json_metadata(project_path, "movie", [{
        "filename": filename,
        "title": "Film One",
        "media_id": media_id,
    }])
    write_shotlist(
        project_path,
        filename,
        "movie",
        [
            {
                "Scene": "1",
                "start_time": "00:00:00.000",
                "end_time": "00:00:01.000",
                "start_frame": index * 101,
                "end_frame": index * 101 + 100,
                "shot_id": shot_id,
            }
            for index, shot_id in enumerate(shot_ids.values())
        ],
    )
    annotations = {
        shot_ids["coat_one"]: {"objects": ["coat"]},
        shot_ids["coat_two"]: {"objects": ["black coat"]},
        shot_ids["yellow_exact"]: {"objects": ["yellow coat"]},
        shot_ids["yellow_split"]: {"objects": ["yellow", "coat"]},
        shot_ids["independent_words"]: {"objects": ["yellow hat", "blue coat"]},
        shot_ids["plural"]: {"objects": ["arm bands"], "wearing": ["coat"]},
        shot_ids["zero_results"]: {"objects": ["barrel"]},
    }
    annotation_path = project_path / "data" / "annotations" / "shots" / "movie" / "Film One.annotations.json"
    annotation_path.parent.mkdir(parents=True, exist_ok=True)
    annotation_path.write_text(
        json.dumps([
            {"shot": {"shot_id": shot_id, "annotation": annotation}}
            for shot_id, annotation in annotations.items()
        ]),
        encoding="utf-8",
    )
    source_frame = project_path / "media" / "frames" / "best" / "movie" / "Film One" / "frame_000000.png"
    _write_png(source_frame, (220, 220, 220, 255))
    return filename, media_id, shot_ids, source_frame


def test_canonical_search_audit_uses_real_search_and_preserves_live_inputs(tmp_path, monkeypatch):
    filename, media_id, shot_ids, source_frame = _make_project(tmp_path)
    catalog_json_paths = [
        _write_catalog_object(
            tmp_path,
            filename=filename,
            media_id=media_id,
            shot_id=shot_ids["coat_one"],
            frame=10,
            label="coat",
            field="objects",
            object_index=1,
            source_frame=source_frame,
        ),
        _write_catalog_object(
            tmp_path,
            filename=filename,
            media_id=media_id,
            shot_id=shot_ids["coat_two"],
            frame=120,
            label="coat",
            field="objects",
            object_index=2,
            source_frame=source_frame,
        ),
        _write_catalog_object(
            tmp_path,
            filename=filename,
            media_id=media_id,
            shot_id=shot_ids["yellow_exact"],
            frame=210,
            label="yellow coat",
            field="objects",
            object_index=3,
            source_frame=source_frame,
        ),
        _write_catalog_object(
            tmp_path,
            filename=filename,
            media_id=media_id,
            shot_id=shot_ids["yellow_split"],
            frame=310,
            label="yellow coat",
            field="objects",
            object_index=4,
            source_frame=source_frame,
        ),
        _write_catalog_object(
            tmp_path,
            filename=filename,
            media_id=media_id,
            shot_id=shot_ids["independent_words"],
            frame=410,
            label="yellow coat",
            field="objects",
            object_index=5,
            source_frame=source_frame,
        ),
        _write_catalog_object(
            tmp_path,
            filename=filename,
            media_id=media_id,
            shot_id=shot_ids["plural"],
            frame=510,
            label="arm band",
            field="objects",
            object_index=6,
            source_frame=source_frame,
        ),
        _write_catalog_object(
            tmp_path,
            filename=filename,
            media_id=media_id,
            shot_id=shot_ids["plural"],
            frame=520,
            label="coat",
            field="wearing",
            object_index=8,
            source_frame=source_frame,
        ),
        _write_catalog_object(
            tmp_path,
            filename=filename,
            media_id=media_id,
            shot_id=shot_ids["zero_results"],
            frame=610,
            label="wooden post",
            field="objects",
            object_index=7,
            source_frame=source_frame,
        ),
        _write_catalog_object(
            tmp_path,
            filename=filename,
            media_id=media_id,
            shot_id="",
            frame=620,
            label="coat",
            field="objects",
            object_index=9,
            source_frame=source_frame,
        ),
    ]
    annotation_path = tmp_path / "data" / "annotations" / "shots" / "movie" / "Film One.annotations.json"
    live_inputs_before = {
        path: path.read_bytes()
        for path in [*catalog_json_paths, annotation_path]
    }

    calls: list[tuple[str, str, str]] = []
    real_search_shots = audit_module.search_shots
    source_loads: list[tuple[str, str]] = []
    real_load_search_source = search_mod._load_search_source

    def tracking_search_shots(**kwargs):
        calls.append((kwargs["media_type"], kwargs["field"], kwargs["query"]))
        return real_search_shots(**kwargs)

    def tracking_load_search_source(project_path, media_type, filename):
        source_loads.append((media_type, filename))
        return real_load_search_source(project_path, media_type, filename)

    monkeypatch.setattr(audit_module, "search_shots", tracking_search_shots)
    monkeypatch.setattr(search_mod, "_load_search_source", tracking_load_search_source)
    report = audit_silhouette_canonical_search(tmp_path, sample_size=10)

    rows = list(csv.DictReader(Path(report["artifacts"]["records_csv"]).open(encoding="utf-8")))
    classifications = {row["shot_id"]: row["classification"] for row in rows}
    assert classifications[shot_ids["coat_one"]] == VALID
    assert classifications[shot_ids["coat_two"]] == VALID
    assert classifications[shot_ids["yellow_exact"]] == VALID
    assert classifications[shot_ids["yellow_split"]] == QUESTIONABLE
    assert classifications[shot_ids["independent_words"]] == QUESTIONABLE
    assert classifications[shot_ids["plural"]] == VALID
    assert classifications[shot_ids["zero_results"]] == QUESTIONABLE
    assert classifications[""] == UNVERIFIABLE

    split_row = next(row for row in rows if row["shot_id"] == shot_ids["yellow_split"])
    assert split_row["original_annotation_values"] == '["yellow", "coat"]'
    assert split_row["historical_search_label"] == "yellow coat"
    assert split_row["canonical_search_match"] == "False"
    assert split_row["canonical_search_result_count"] == "1"
    assert split_row["canonical_matching_shot_ids"] == json.dumps([shot_ids["yellow_exact"]])

    assert sorted(calls) == [
        ("movie", "objects", "arm band"),
        ("movie", "objects", "coat"),
        ("movie", "objects", "wooden post"),
        ("movie", "objects", "yellow coat"),
        ("movie", "wearing", "coat"),
    ]
    assert report["canonical_search"]["executed_queries"] == 5
    assert source_loads == [("movie", filename)]
    assert report["summary"] == {
        "total_records": 9,
        "valid": 5,
        "questionable": 3,
        "unverifiable": 1,
        "valid_percentage": 55.56,
        "questionable_percentage": 33.33,
    }
    assert report["live_data_safety"] == {
        "enforcement": "audit code writes only generated artifacts beneath outputs/tests",
        "live_silhouette_data_modified": "NO",
        "live_png_data_modified": "NO",
        "live_annotations_modified": "NO",
    }
    assert len(list((Path(report["artifacts"]["examples_dir"])).glob("*/panel.png"))) == 2
    assert all(path.read_bytes() == before for path, before in live_inputs_before.items())


def test_canonical_search_audit_probe_is_bounded_to_known_labels(tmp_path):
    filename, media_id, shot_ids, source_frame = _make_project(tmp_path)
    _write_catalog_object(
        tmp_path,
        filename=filename,
        media_id=media_id,
        shot_id=shot_ids["coat_one"],
        frame=10,
        label="coat",
        field="objects",
        object_index=1,
        source_frame=source_frame,
    )
    _write_catalog_object(
        tmp_path,
        filename=filename,
        media_id=media_id,
        shot_id=shot_ids["plural"],
        frame=510,
        label="arm band",
        field="objects",
        object_index=2,
        source_frame=source_frame,
    )

    report = audit_silhouette_canonical_search(tmp_path, probe=True)

    assert Path(report["output_dir"]) == default_output_dir(tmp_path, probe=True)
    assert report["probe"] is True
    assert report["summary"]["total_records"] == 2
    assert report["summary"]["valid"] == 2
    assert report["summary"]["questionable"] == 0


def test_prepared_search_corpus_matches_uncached_canonical_search(tmp_path):
    _filename, _media_id, _shot_ids, _source_frame = _make_project(tmp_path)
    queries = ["coat", "yellow coat", "arm band", "wooden post"]
    corpus = search_mod.SearchCorpus(str(tmp_path))
    corpus.prepare_queries("movie", {("objects", query) for query in queries})

    for query in queries:
        uncached = search_mod.search_shots(
            query=query,
            scopes=None,
            field="objects",
            limit=None,
            limit_per_item=None,
            use_all=True,
            project_path=str(tmp_path),
            media_type="movie",
        )
        prepared = search_mod.search_shots(
            query=query,
            scopes=None,
            field="objects",
            limit=None,
            limit_per_item=None,
            use_all=True,
            project_path=str(tmp_path),
            media_type="movie",
            corpus=corpus,
        )
        assert prepared == uncached


def test_representative_group_selection_preserves_priority_field_coverage():
    def group(field, label, count):
        return {"field": field, "label": label, "record_count": count, "representative": {}}

    selected = audit_module._select_representative_groups(
        [
            group("objects", "wooden plank", 100),
            group("objects", "wooden post", 90),
            group("wearing", "arm band", 5),
            group("animals", "black horse", 3),
            group("setting", "dusty street", 2),
        ],
        sample_size=5,
    )

    assert {(item["field"], item["label"]) for item in selected} == {
        ("objects", "wooden plank"),
        ("objects", "wooden post"),
        ("wearing", "arm band"),
        ("animals", "black horse"),
        ("setting", "dusty street"),
    }


def test_bounded_diagnostic_sample_retains_matched_source_shot():
    refs = [
        {"filename": "Film One.mp4", "shot_id": f"shot_{index:05d}"}
        for index in range(1, 12)
    ]
    source_identity = ("Film One.mp4", "shot_00011")

    ids, sampled_refs = audit_module._diagnostic_result_sample(
        {
            "refs": refs[:10],
            "source_identities": {source_identity},
        },
        source_identity,
    )

    assert ids[0] == "shot_00011"
    assert sampled_refs[0] == {"filename": "Film One.mp4", "shot_id": "shot_00011"}
    assert len(ids) == 10


def test_canonical_search_audit_rejects_output_outside_test_artifacts(tmp_path):
    _make_project(tmp_path)

    with pytest.raises(ValueError, match="outputs/tests"):
        audit_silhouette_canonical_search(tmp_path, output_dir=tmp_path / "unsafe-output")


def test_index_silhouette_canonical_search_audit_dispatch(monkeypatch, tmp_path, capsys):
    calls = {}

    def fake_audit(project_path, **kwargs):
        calls["project_path"] = project_path
        calls.update(kwargs)
        return {
            "summary": {
                "total_records": 3,
                "valid": 2,
                "questionable": 1,
                "unverifiable": 0,
                "questionable_percentage": 33.33,
            },
            "live_data_safety": {
                "live_silhouette_data_modified": "NO",
                "live_png_data_modified": "NO",
                "live_annotations_modified": "NO",
            },
            "artifacts": {"report_md": "audit/report.md"},
        }

    monkeypatch.setattr("tool.prefs.get", lambda key, default=None: str(tmp_path) if key == "path" else default)
    monkeypatch.setattr(audit_module, "audit_silhouette_canonical_search", fake_audit)
    monkeypatch.setitem(__import__("sys").modules, "services.silhouette_canonical_search_audit", audit_module)

    cli._silhouette_canonical_search_audit(
        SimpleNamespace(media="both", output_dir="outputs/tests/custom", sample_size=9, probe=True)
    )

    assert calls == {
        "project_path": str(tmp_path),
        "media_type": "both",
        "output_dir": "outputs/tests/custom",
        "sample_size": 9,
        "probe": True,
    }
    output = capsys.readouterr().out
    assert "Canonical search audit: records=3" in output
    assert "LIVE SILHOUETTE DATA MODIFIED = NO" in output