import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import call, patch

import cli
from services.illustration_index import (
    index_path,
    invalidate_for_record,
    invalidate_index,
    load_index,
    query_facets,
    query_field_counts,
    query_page,
    query_untyped_records,
    rebuild_index,
)


def test_silhouette_index_reports_missing_ready_and_stale(tmp_path):
    label_dir = tmp_path / "data" / "silhouettes" / "catalog" / "movie" / "film" / "horse"
    label_dir.mkdir(parents=True)
    metadata_path = label_dir / "object_0001.json"
    metadata_path.write_text(json.dumps({
        "media_type": "movie",
        "filename_stem": "film",
        "label": "horse",
        "field": "animals",
        "png": "object_0001.png",
    }), encoding="utf-8")

    assert load_index(tmp_path, "silhouettes", "movie")["status"] == "missing"
    result = rebuild_index(tmp_path, "silhouettes", "movie")
    assert result["count"] == 1

    loaded = load_index(tmp_path, "silhouettes", "movie")
    assert loaded["status"] == "ready"
    assert loaded["count"] == 1
    assert "items" not in loaded
    assert query_page(tmp_path, "silhouettes", "movie")["records"][0]["path"] == metadata_path

    invalidate_index(tmp_path, "silhouettes", "movie")
    stale = load_index(tmp_path, "silhouettes", "movie")
    assert stale["status"] == "stale"
    assert stale["usable"] is True
    assert stale["count"] == 1
    assert query_field_counts(tmp_path, "silhouettes", "movie") == {
        "status": "stale",
        "count": 1,
        "fields": [{"field": "animals", "count": 1}],
    }
    assert query_page(tmp_path, "silhouettes", "movie")["records"][0]["path"] == metadata_path


def test_silhouette_index_filters_by_provenance_state(tmp_path):
    records = [
        {
            "filename_stem": "film",
            "field": "objects",
            "label": "coat",
            "search_provenance": {
                "state": "valid",
                "reason": "single_word_label",
                "audit_version": "semantic-v1",
            },
        },
        {
            "filename_stem": "film",
            "field": "objects",
            "label": "yellow coat",
            "search_provenance": {
                "state": "questionable",
                "reason": "multi_word_not_exact_annotation_value",
                "audit_version": "semantic-v1",
            },
        },
        {
            "filename_stem": "film",
            "field": "objects",
            "label": "unclassified",
        },
    ]
    with patch("services.illustration_index._scan_silhouettes", return_value=records):
        rebuild_index(tmp_path, "silhouettes", "movie")

    valid = query_page(tmp_path, "silhouettes", "movie", provenance_state="valid")
    questionable = query_page(
        tmp_path, "silhouettes", "movie", provenance_state="questionable"
    )
    all_records = query_page(tmp_path, "silhouettes", "movie")

    assert [record["label"] for record in valid["records"]] == ["coat"]
    assert valid["records"][0]["search_provenance"]["state"] == "valid"
    assert [record["label"] for record in questionable["records"]] == ["yellow coat"]
    assert questionable["records"][0]["search_provenance"]["state"] == "questionable"
    assert sorted(record["label"] for record in all_records["records"]) == [
        "coat",
        "unclassified",
        "yellow coat",
    ]


def test_engraving_index_contains_only_generated_records(tmp_path):
    mode_dir = tmp_path / "data" / "engravings" / "catalog" / "movie" / "film" / "horse" / "object_0001" / "isolated"
    mode_dir.mkdir(parents=True)
    raw_path = mode_dir / "raw.png"
    raw_path.write_bytes(b"png")
    metadata_path = mode_dir / "engraving.json"
    metadata_path.write_text(json.dumps({
        "status": "generated",
        "model": "test-model",
    }), encoding="utf-8")

    pending_dir = mode_dir.parent / "frame"
    pending_dir.mkdir()
    (pending_dir / "engraving.json").write_text(
        json.dumps({"status": "pending"}), encoding="utf-8"
    )

    result = rebuild_index(tmp_path, "engravings", "movie")
    assert result["count"] == 1
    loaded = load_index(tmp_path, "engravings", "movie")
    assert loaded["status"] == "ready"
    record = query_page(tmp_path, "engravings", "movie")["records"][0]
    assert record["path"] == metadata_path
    assert record["raw_png"] == raw_path
    assert record["model"] == "test-model"


def test_silhouette_field_counts_use_indexed_rows_and_deterministic_order(tmp_path):
    records = [
        {"filename_stem": "film", "field": "objects", "label": "hat"},
        {"filename_stem": "film", "field": "animals", "label": "horse"},
        {"filename_stem": "film", "field": "objects", "label": "lamp"},
        {"filename_stem": "film", "field": "humans", "label": "rider"},
        {"filename_stem": "film", "field": "animals", "label": "dog"},
    ]
    with patch("services.illustration_index._scan_silhouettes", return_value=records):
        rebuild_index(tmp_path, "silhouettes", "movie")

    assert query_field_counts(tmp_path, "silhouettes", "movie") == {
        "status": "ready",
        "count": 5,
        "fields": [
            {"field": "animals", "count": 2},
            {"field": "objects", "count": 2},
            {"field": "humans", "count": 1},
        ],
    }


def test_query_untyped_records_returns_only_synthetic_index_rows(tmp_path):
    records = [
        {"filename_stem": "film", "field": None, "label": "horse", "frame": 12},
        {"filename_stem": "film", "field": "animals", "label": "dog", "frame": 24},
    ]
    with patch("services.illustration_index._scan_silhouettes", return_value=records):
        rebuild_index(tmp_path, "silhouettes", "movie")

    result = query_untyped_records(tmp_path, "silhouettes", "movie")

    assert result["status"] == "ready"
    assert result["total"] == 1
    assert [record["frame"] for record in result["records"]] == [12]

    invalidate_index(tmp_path, "silhouettes", "movie")
    assert query_untyped_records(
        tmp_path, "silhouettes", "movie",
    )["status"] == "stale"


def test_engraving_field_counts_use_source_silhouette_provenance(tmp_path):
    def write_engraving(object_id, mode, field):
        mode_dir = (
            tmp_path / "data" / "engravings" / "catalog" / "movie"
            / "film" / "horse" / object_id / mode
        )
        mode_dir.mkdir(parents=True)
        (mode_dir / "raw.png").write_bytes(b"png")
        (mode_dir / "engraving.json").write_text(json.dumps({
            "schema_version": "2",
            "status": "generated",
            "mode": mode,
            "field": "must-not-override-source",
            "source": {
                "silhouette_json": (
                    f"data/silhouettes/catalog/movie/film/horse/{object_id}.json"
                ),
            },
            "silhouette": {"field": field, "label": "horse"},
        }), encoding="utf-8")

    write_engraving("object_0001", "isolated", "animals")
    write_engraving("object_0001", "frame", "animals")
    write_engraving("object_0002", "isolated", "objects")

    rebuild_index(tmp_path, "engravings", "movie")

    assert query_field_counts(tmp_path, "engravings", "movie") == {
        "status": "ready",
        "count": 3,
        "fields": [
            {"field": "animals", "count": 2},
            {"field": "objects", "count": 1},
        ],
    }


def test_field_counts_accept_a_valid_empty_index(tmp_path):
    with patch("services.illustration_index._scan_silhouettes", return_value=[]):
        rebuild_index(tmp_path, "silhouettes", "movie")

    assert query_field_counts(tmp_path, "silhouettes", "movie") == {
        "status": "ready", "count": 0, "fields": [],
    }


def test_silhouette_field_counts_use_synthetic_untyped_for_missing_field(tmp_path):
    with patch(
        "services.illustration_index._scan_silhouettes",
        return_value=[{"filename_stem": "film", "label": "horse"}],
    ):
        rebuild_index(tmp_path, "silhouettes", "movie")

    result = query_field_counts(tmp_path, "silhouettes", "movie")

    assert result == {
        "status": "ready",
        "count": 1,
        "fields": [{"field": "<untyped>", "count": 1, "synthetic": True}],
    }


def test_engraving_field_counts_reject_missing_source_field(tmp_path):
    records = [{"filename_stem": "film", "label": "horse"}]
    with patch("services.illustration_index._scan_engravings", return_value=records):
        rebuild_index(tmp_path, "engravings", "movie")

    assert query_field_counts(tmp_path, "engravings", "movie") == {
        "status": "error", "count": 0, "fields": [],
    }


def test_schema_change_marks_index_stale(tmp_path):
    rebuild_index(tmp_path, "silhouettes", "movie")
    path = index_path(tmp_path, "silhouettes", "movie")
    import sqlite3
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE meta SET value = '-1' WHERE key = 'schema_version'")
        connection.commit()

    stale = load_index(tmp_path, "silhouettes", "movie")
    assert stale["status"] == "stale"
    assert stale["usable"] is False
    assert query_field_counts(tmp_path, "silhouettes", "movie") == {
        "status": "stale", "count": 0, "fields": [], "usable": False,
    }


def test_legacy_monolithic_index_requires_rebuild(tmp_path):
    legacy_path = (
        tmp_path / "data" / "indexes" / "illustration"
        / "movie-silhouettes.json"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("{}", encoding="utf-8")

    assert load_index(tmp_path, "silhouettes", "movie")["status"] == "stale"

    with patch("services.illustration_index._scan_silhouettes", return_value=[]):
        rebuild_index(tmp_path, "silhouettes", "movie")
    assert not legacy_path.exists()


def test_index_queries_facets_and_pages_without_materializing_catalog(tmp_path):
    records = [
        {
            "filename_stem": "Film {tmdb-12}",
            "field": "animals",
            "label": "horse",
        },
        {
            "filename_stem": "Film {tmdb-12}",
            "field": "objects",
            "label": "hat",
        },
    ]
    with patch("services.illustration_index._scan_silhouettes", return_value=records):
        rebuild_index(tmp_path, "silhouettes", "movie")

    path = index_path(tmp_path, "silhouettes", "movie")
    loaded = load_index(tmp_path, "silhouettes", "movie")
    facets = query_facets(tmp_path, "silhouettes", "movie")
    animals = query_page(
        tmp_path, "silhouettes", "movie", field="animals", limit=1
    )

    assert path.suffix == ".sqlite3"
    assert loaded["count"] == 2
    assert "items" not in loaded
    assert facets["titles"] == ["Film"]
    assert facets["fields"] == ["animals", "objects"]
    assert facets["labels"] == [
        {"label": "hat", "count": 1},
        {"label": "horse", "count": 1},
    ]
    assert animals["total"] == 1
    assert animals["records"][0]["label"] == "horse"


def test_silhouette_index_derives_object_id_and_engraved_first_sort(tmp_path):
    catalog = tmp_path / "data" / "silhouettes" / "catalog" / "movie" / "film" / "horse"
    catalog.mkdir(parents=True)
    for object_id in ("object_0001", "object_0002"):
        (catalog / f"{object_id}.json").write_text(json.dumps({
            "media_type": "movie", "filename_stem": "film",
            "label": "horse", "field": "animals",
        }), encoding="utf-8")
    engraving = (
        tmp_path / "data" / "engravings" / "catalog" / "movie" / "film"
        / "horse" / "object_0002" / "isolated"
    )
    engraving.mkdir(parents=True)
    (engraving / "engraving.json").write_text(
        json.dumps({"status": "generated"}), encoding="utf-8"
    )

    rebuild_index(tmp_path, "silhouettes", "movie")
    result = query_page(
        tmp_path, "silhouettes", "movie", sort_keys=["engraved_first"]
    )

    assert [Path(record["path"]).stem for record in result["records"]] == [
        "object_0002", "object_0001"
    ]


def test_record_path_invalidation_infers_project_and_media_type(tmp_path):
    rebuild_index(tmp_path, "silhouettes", "gameplay")
    record_path = (
        tmp_path / "data" / "silhouettes" / "catalog" / "gameplay"
        / "film" / "horse" / "object_0001.json"
    )

    assert invalidate_for_record(record_path, "silhouettes") is True
    assert load_index(tmp_path, "silhouettes", "gameplay")["status"] == "stale"


def test_cli_illustration_index_rebuilds_both_sources(tmp_path, capsys):
    results = {
        "silhouettes": {"status": "ready", "count": 7},
        "engravings": {"status": "ready", "count": 3},
    }
    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.illustration_index.rebuild_all", return_value=results) as rebuild,
    ):
        cli._index_illustration(Namespace(media="gameplay"))

    rebuild.assert_called_once_with(str(tmp_path), "gameplay")
    assert capsys.readouterr().out.splitlines() == [
        "Silhouettes: 7 indexed",
        "Engravings: 3 indexed",
    ]


def test_parser_exposes_illustration_index_command():
    args = cli.build_parser().parse_args(["index", "illustration", "--media", "gameplay"])
    assert args.index_subcommand == "illustration"
    assert args.media == "gameplay"


def test_cli_illustration_index_rebuilds_both_media_types(tmp_path, capsys):
    results = {
        "silhouettes": {"status": "ready", "count": 7},
        "engravings": {"status": "ready", "count": 3},
    }
    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.illustration_index.rebuild_all", return_value=results) as rebuild,
    ):
        cli._index_illustration(Namespace(media="both"))

    assert rebuild.call_args_list == [
        call(str(tmp_path), "movie"),
        call(str(tmp_path), "gameplay"),
    ]
    assert capsys.readouterr().out.splitlines() == [
        "Movie Silhouettes: 7 indexed",
        "Movie Engravings: 3 indexed",
        "Gameplay Silhouettes: 7 indexed",
        "Gameplay Engravings: 3 indexed",
    ]


def test_parser_accepts_both_for_illustration_index():
    args = cli.build_parser().parse_args([
        "index", "illustration", "--media", "both",
    ])

    assert args.media == "both"


def test_parser_exposes_vocabulary_family_selection():
    args = cli.build_parser().parse_args([
        "index", "vocabulary", "--media", "gameplay", "--family", "derived",
    ])

    assert args.family == "derived"