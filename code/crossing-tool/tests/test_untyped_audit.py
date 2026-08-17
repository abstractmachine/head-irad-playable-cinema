import json
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import cli
from data.media_id import build_shot_id
from data.metadata import save_json_metadata
from data.shotlist import write_shotlist
from services.untyped_audit import generate_untyped_audit


def _seed_media_and_shotlist(tmp_path):
    filename = "Film One.mp4"
    media_id = "file_film_one"
    save_json_metadata(tmp_path, "movie", [{
        "filename": filename,
        "title": "Film One",
        "year": 2026,
        "media_id": media_id,
    }])
    shots = [
        {
            "Scene": "1",
            "start_time": "00:00:10.000",
            "end_time": "00:00:14.000",
            "start_frame": 0,
            "end_frame": 100,
            "shot_id": build_shot_id(media_id, 0, 100),
        },
        {
            "Scene": "1",
            "start_time": "00:00:20.000",
            "end_time": "00:00:24.000",
            "start_frame": 101,
            "end_frame": 201,
            "shot_id": build_shot_id(media_id, 101, 201),
        },
    ]
    write_shotlist(tmp_path, filename, "movie", shots)
    return filename, shots


def test_generate_untyped_shot_audit_groups_filename_and_start_timecodes(tmp_path):
    filename, shots = _seed_media_and_shotlist(tmp_path)
    annotation_path = (
        tmp_path / "data" / "annotations" / "shots" / "movie"
        / "Film One.annotations.json"
    )
    annotation_path.parent.mkdir(parents=True)
    annotation_path.write_text(json.dumps([
        {
            "shot": {
                "shot_id": shots[0]["shot_id"],
                "annotation": {},
            },
        },
        {
            "shot": {
                "shot_id": shots[1]["shot_id"],
                "annotation": {"type": "diegetic"},
            },
        },
    ]), encoding="utf-8")

    result = generate_untyped_audit(
        tmp_path,
        "shot",
        now=datetime(2026, 8, 17, 13, 45, 6),
    )

    assert result == (
        tmp_path / "outputs" / "audits"
        / "untyped-shot-2026-08-17-13-45-06.md"
    )
    assert result.read_text(encoding="utf-8") == (
        "# untyped\n"
        "## shot\n\n"
        f"# {filename}\n"
        f"- {shots[0]['shot_id']} (00:00:10.000)\n"
    )


def test_generate_untyped_silhouette_audit_interpolates_frame_timecodes(
    tmp_path, monkeypatch,
):
    filename, shots = _seed_media_and_shotlist(tmp_path)

    def query(_project, source, media_type):
        assert source == "silhouettes"
        if media_type == "movie":
            return {
                "status": "ready",
                "usable": True,
                "records": [{
                    "filename": filename,
                    "filename_stem": "Film One",
                    "frame": 25,
                }],
            }
        return {"status": "missing", "records": []}

    monkeypatch.setattr(
        "services.illustration_index.query_untyped_records", query,
    )

    result = generate_untyped_audit(
        tmp_path,
        "silhouettes",
        now=datetime(2026, 8, 17, 13, 45, 7),
    )

    assert result == (
        tmp_path / "outputs" / "audits"
        / "untyped-silhouettes-2026-08-17-13-45-07.md"
    )
    assert result.read_text(encoding="utf-8") == (
        "# untyped\n"
        "## silhouettes\n\n"
        f"# {filename}\n"
        f"- {shots[0]['shot_id']} (00:00:11.000)\n"
    )


def test_parser_exposes_untyped_audit_sources():
    args = cli.build_parser().parse_args([
        "index", "untyped", "--source", "silhouettes",
    ])

    assert args.index_subcommand == "untyped"
    assert args.source == "silhouettes"


def test_cli_untyped_generates_then_opens_report(tmp_path, capsys):
    report_path = Path(tmp_path) / "outputs" / "audits" / "report.md"
    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch(
            "services.untyped_audit.generate_untyped_audit",
            return_value=report_path,
        ) as generate,
        patch.object(cli, "_open_with_default_app") as open_report,
    ):
        cli._index_untyped(Namespace(source="shot"))

    generate.assert_called_once_with(str(tmp_path), "shot")
    open_report.assert_called_once_with(report_path)
    assert capsys.readouterr().out.strip() == f"Saved: {report_path}"
