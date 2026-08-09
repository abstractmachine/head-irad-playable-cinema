import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import cli
from services.illustration_index import (
    index_path,
    invalidate_for_record,
    invalidate_index,
    load_index,
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
    assert loaded["items"][0]["path"] == metadata_path

    invalidate_index(tmp_path, "silhouettes", "movie")
    assert load_index(tmp_path, "silhouettes", "movie")["status"] == "stale"


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
    assert loaded["items"][0]["path"] == metadata_path
    assert loaded["items"][0]["raw_png"] == raw_path
    assert loaded["items"][0]["model"] == "test-model"


def test_schema_change_marks_index_stale(tmp_path):
    rebuild_index(tmp_path, "silhouettes", "movie")
    path = index_path(tmp_path, "silhouettes", "movie")
    lines = path.read_text(encoding="utf-8").splitlines()
    document = json.loads(lines[0])
    document["schema_version"] = -1
    lines[0] = json.dumps(document)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert load_index(tmp_path, "silhouettes", "movie")["status"] == "stale"


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


def test_index_persists_filter_cache_in_streaming_header(tmp_path):
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
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    loaded = load_index(tmp_path, "silhouettes", "movie")

    assert path.suffix == ".jsonl"
    assert len(lines) == 3
    assert header["filter_cache"]["films"] == ["Film"]
    assert loaded["filter_cache"]["fields"] == {"animals", "objects"}
    assert loaded["filter_cache"]["counts"] == {"horse": 1, "hat": 1}


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