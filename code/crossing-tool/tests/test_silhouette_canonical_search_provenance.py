import csv
import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

import cli
from services.silhouette_catalog import catalog_item_dir
from services.silhouette_canonical_search_provenance import (
    CANONICAL_SEARCH_PROVENANCE_AUDIT_VERSION,
    default_migration_report_dir,
    migrate_canonical_search_provenance,
)


_FIELDS = [
    "media_type", "media_id", "filename", "shot_id", "canonical_source_shot_id",
    "source_shot_identity", "field", "historical_label", "historical_search_label",
    "historical_json_path", "original_annotation_values", "canonical_search_match",
    "source_shot_returned", "canonical_search_result_count", "canonical_matching_shot_ids",
    "canonical_matching_shot_refs", "canonical_matching_shot_ids_truncated", "classification",
    "classification_reason",
]


def _write_catalog(
    project: Path,
    *,
    index: int,
    label: str,
    field: str = "objects",
    media_type: str = "movie",
) -> tuple[Path, Path, dict]:
    filename_stem = "film" if media_type == "movie" else "gameplay"
    media_id = "tmdb_1" if media_type == "movie" else "game_1"
    directory = catalog_item_dir(str(project), media_type, filename_stem, label)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"object_{index:04d}.json"
    png_path = directory / f"object_{index:04d}.png"
    png_bytes = b"historical-png"
    png_path.write_bytes(png_bytes)
    record = {
        "media_type": media_type,
        "media_id": media_id,
        "filename": f"{filename_stem}.mp4",
        "filename_stem": filename_stem,
        "shot_id": f"{media_id}@f{index:06d}-f{index + 1:06d}",
        "frame": index,
        "field": field,
        "label": label,
        "png": png_path.name,
        "custom_field": "preserve-me",
    }
    json_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return json_path, png_path, record | {"_png_bytes": png_bytes}


def _audit_row(path: Path, record: dict, classification: str) -> dict[str, str]:
    matched = classification == "VALID"
    return {
        "media_type": record["media_type"],
        "media_id": record["media_id"],
        "filename": record["filename"],
        "shot_id": record["shot_id"],
        "canonical_source_shot_id": record["shot_id"],
        "source_shot_identity": json.dumps({
            "media_type": record["media_type"],
            "filename": record["filename"],
            "shot_id": record["shot_id"],
        }),
        "field": record["field"],
        "historical_label": record["label"],
        "historical_search_label": record["label"],
        "historical_json_path": str(path),
        "original_annotation_values": json.dumps([record["label"]]),
        "canonical_search_match": "" if classification == "UNVERIFIABLE" else str(matched),
        "source_shot_returned": "" if classification == "UNVERIFIABLE" else str(matched),
        "canonical_search_result_count": "" if classification == "UNVERIFIABLE" else str(1 if matched else 0),
        "canonical_matching_shot_ids": json.dumps([record["shot_id"]] if matched else []),
        "canonical_matching_shot_refs": json.dumps([]),
        "canonical_matching_shot_ids_truncated": "" if classification == "UNVERIFIABLE" else "False",
        "classification": classification,
        "classification_reason": f"canonical-{classification.casefold()}",
    }


def _write_audit(project: Path, rows: list[dict[str, str]]) -> Path:
    audit_dir = project / "outputs" / "tests" / "silhouette-canonical-search-audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    with (audit_dir / "canonical_search_records.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "total_records": len(rows),
        "valid": sum(row["classification"] == "VALID" for row in rows),
        "questionable": sum(row["classification"] == "QUESTIONABLE" for row in rows),
        "unverifiable": sum(row["classification"] == "UNVERIFIABLE" for row in rows),
    }
    (audit_dir / "report.json").write_text(json.dumps({
        "generated_at": "2026-09-01T00:00:00+00:00",
        "project_path": str(project.resolve()),
        "output_dir": str(audit_dir.resolve()),
        "probe": False,
        "media_types": sorted({row["media_type"] for row in rows}),
        "canonical_search": {"service": "services.search.search_shots", "failed_queries": 0},
        "summary": summary,
    }), encoding="utf-8")
    return audit_dir


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("classification", "expected_state", "expected_match"),
    [
        ("VALID", "valid", True),
        ("QUESTIONABLE", "questionable", False),
        ("UNVERIFIABLE", "unverifiable", None),
    ],
)
def test_migration_copies_canonical_classification_without_search_or_old_provenance(
    tmp_path, classification, expected_state, expected_match
):
    field = "" if classification == "UNVERIFIABLE" else "objects"
    json_path, png_path, record = _write_catalog(
        tmp_path, index=1, label="coat", field=field
    )
    on_disk_record = {key: value for key, value in record.items() if key != "_png_bytes"}
    if classification == "UNVERIFIABLE":
        on_disk_record["shot_id"] = ""
    on_disk_record["search_provenance"] = {
        "state": "questionable", "audit_version": "semantic-v1"
    }
    json_path.write_text(json.dumps(on_disk_record, indent=2), encoding="utf-8")
    audit_row = _audit_row(json_path, on_disk_record, classification)
    if classification == "UNVERIFIABLE":
        audit_row["source_shot_identity"] = json.dumps({
            "media_type": "movie", "filename": "film.mp4", "shot_id": None,
        })
    audit_dir = _write_audit(tmp_path, [audit_row])

    with (
        patch("services.search.search_shots", side_effect=AssertionError("must not search")),
        patch("services.illustration_index.rebuild_index", return_value={"status": "ready", "count": 1}) as rebuild,
    ):
        result = migrate_canonical_search_provenance(
            tmp_path, audit_dir=audit_dir, media_type="movie", dry_run=False
        )

    assert result["safe_to_apply"] is True
    assert result["applied"] is True
    assert result["updated"] == 1
    rebuild.assert_called_once_with(tmp_path.resolve(), "silhouettes", "movie")
    after = _read_json(json_path)
    provenance = after["search_provenance"]
    assert provenance["state"] == expected_state
    assert provenance["method"] == "canonical_search"
    assert provenance["audit_version"] == CANONICAL_SEARCH_PROVENANCE_AUDIT_VERSION
    assert provenance["canonical_search_match"] is expected_match
    assert after["custom_field"] == "preserve-me"
    assert png_path.read_bytes() == record["_png_bytes"]
    report = _read_json(default_migration_report_dir(tmp_path) / "report.json")
    assert report["audit_records_csv"]["sha256"]


def test_migration_is_idempotent_after_authoritative_state_is_written(tmp_path):
    json_path, _png_path, record = _write_catalog(tmp_path, index=1, label="coat")
    audit_dir = _write_audit(tmp_path, [_audit_row(json_path, record, "VALID")])

    with patch("services.illustration_index.rebuild_index", return_value={"status": "ready"}):
        first = migrate_canonical_search_provenance(
            tmp_path, audit_dir=audit_dir, media_type="movie", dry_run=False
        )
        second = migrate_canonical_search_provenance(
            tmp_path, audit_dir=audit_dir, media_type="movie", dry_run=False
        )

    assert first["updated"] == 1
    assert second["updated"] == 0
    assert second["already_authoritative"] == 1


def test_migration_blocks_duplicate_rows_and_stale_archive_identity(tmp_path):
    json_path, _png_path, record = _write_catalog(tmp_path, index=1, label="coat")
    audit_dir = _write_audit(tmp_path, [_audit_row(json_path, record, "VALID")] * 2)

    duplicate = migrate_canonical_search_provenance(
        tmp_path, audit_dir=audit_dir, media_type="movie", dry_run=False
    )

    assert duplicate["safe_to_apply"] is False
    assert duplicate["duplicate_audit_records"] == 1
    assert "search_provenance" not in _read_json(json_path)

    audit_dir = _write_audit(tmp_path, [_audit_row(json_path, record, "VALID")])
    stale = _read_json(json_path)
    stale["label"] = "hat"
    json_path.write_text(json.dumps(stale, indent=2), encoding="utf-8")

    mismatch = migrate_canonical_search_provenance(
        tmp_path, audit_dir=audit_dir, media_type="movie", dry_run=False
    )

    assert mismatch["safe_to_apply"] is False
    assert mismatch["identity_mismatches"] == 1
    assert mismatch["valid"] == 1
    assert mismatch["errors"] >= 1
    assert "search_provenance" not in _read_json(json_path)


def test_migration_blocks_report_summary_that_disagrees_with_source_csv(tmp_path):
    json_path, _png_path, record = _write_catalog(tmp_path, index=1, label="coat")
    audit_dir = _write_audit(tmp_path, [_audit_row(json_path, record, "VALID")])
    report_path = audit_dir / "report.json"
    report = _read_json(report_path)
    report["summary"]["valid"] = 0
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = migrate_canonical_search_provenance(
        tmp_path, audit_dir=audit_dir, media_type="movie", dry_run=False
    )

    assert result["safe_to_apply"] is False
    assert result["applied"] is False
    assert "search_provenance" not in _read_json(json_path)


def test_migration_writes_a_blocking_report_for_malformed_audit_rows(tmp_path):
    json_path, _png_path, record = _write_catalog(tmp_path, index=1, label="coat")
    audit_dir = _write_audit(tmp_path, [_audit_row(json_path, record, "VALID")])
    (audit_dir / "canonical_search_records.csv").write_text(
        "media_type\nmovie\n", encoding="utf-8"
    )

    result = migrate_canonical_search_provenance(
        tmp_path, audit_dir=audit_dir, media_type="movie", dry_run=False
    )

    assert result["safe_to_apply"] is False
    assert result["audit_records_complete"] is False
    assert result["errors"] >= 1
    saved = _read_json(default_migration_report_dir(tmp_path) / "report.json")
    assert saved["errors"] >= 1
    assert "search_provenance" not in _read_json(json_path)


def test_migration_blocks_when_live_catalog_contains_an_object_absent_from_audit(tmp_path):
    json_path, _png_path, record = _write_catalog(tmp_path, index=1, label="coat")
    extra_path, _extra_png, _extra_record = _write_catalog(tmp_path, index=2, label="hat")
    audit_dir = _write_audit(tmp_path, [_audit_row(json_path, record, "VALID")])

    result = migrate_canonical_search_provenance(
        tmp_path, audit_dir=audit_dir, media_type="movie", dry_run=False
    )

    assert result["safe_to_apply"] is False
    assert result["applied"] is False
    assert result["unmatched_catalog_records"] == 1
    assert "search_provenance" not in _read_json(json_path)
    assert "search_provenance" not in _read_json(extra_path)


def test_migration_rebuilds_each_selected_media_index_once(tmp_path):
    movie_path, _movie_png, movie = _write_catalog(tmp_path, index=1, label="coat")
    gameplay_path, _gameplay_png, gameplay = _write_catalog(
        tmp_path, index=1, label="horse", media_type="gameplay"
    )
    audit_dir = _write_audit(tmp_path, [
        _audit_row(movie_path, movie, "VALID"),
        _audit_row(gameplay_path, gameplay, "QUESTIONABLE"),
    ])

    with patch(
        "services.illustration_index.rebuild_index",
        return_value={
            "status": "ready",
            "count": 1,
            "path": tmp_path / "data" / "indexes" / "illustration" / "movie-silhouettes.sqlite3",
        },
    ) as rebuild:
        result = migrate_canonical_search_provenance(
            tmp_path, audit_dir=audit_dir, media_type="both", dry_run=False
        )

    assert result["applied"] is True
    assert rebuild.call_args_list[0].args == (tmp_path.resolve(), "silhouettes", "movie")
    assert rebuild.call_args_list[1].args == (tmp_path.resolve(), "silhouettes", "gameplay")
    assert _read_json(movie_path)["search_provenance"]["state"] == "valid"
    assert _read_json(gameplay_path)["search_provenance"]["state"] == "questionable"
    saved = _read_json(default_migration_report_dir(tmp_path) / "report.json")
    assert saved["index_results"]["movie"]["path"].endswith("movie-silhouettes.sqlite3")


def test_cli_dispatches_the_explicit_canonical_migration(tmp_path, capsys):
    report = {
        "dry_run": True,
        "selected_audit_records": 2,
        "selected_catalog_records": 2,
        "valid": 1,
        "questionable": 1,
        "unverifiable": 0,
        "would_update": 2,
        "errors": 0,
        "safe_to_apply": True,
        "applied": False,
        "report_path": str(tmp_path / "outputs" / "tests" / "report.json"),
    }
    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch(
            "services.silhouette_canonical_search_provenance.migrate_canonical_search_provenance",
            return_value=report,
        ) as migrate,
    ):
        cli._silhouette_canonical_search_provenance(
            Namespace(media="both", audit_dir=None, dry_run=True)
        )

    assert migrate.call_args.kwargs == {
        "audit_dir": None,
        "media_type": "both",
        "dry_run": True,
    }
    assert migrate.call_args.args == (str(tmp_path),)
    assert "Canonical provenance migration (dry-run):" in capsys.readouterr().out