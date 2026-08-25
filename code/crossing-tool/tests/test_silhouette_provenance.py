import csv
import json
from pathlib import Path
from argparse import Namespace
from unittest.mock import patch

import pytest
from PIL import Image

import cli
from services.silhouette_catalog import catalog_item_dir
from services.silhouette_provenance import (
    SEARCH_PROVENANCE_AUDIT_VERSION,
    default_audit_dir,
    migrate_search_provenance,
)


def _write_png(path: Path) -> bytes:
    image = Image.new("RGBA", (12, 12), (0, 0, 0, 0))
    for y in range(3, 9):
        for x in range(3, 9):
            image.putpixel((x, y), (255, 255, 255, 255))
    image.save(path)
    return path.read_bytes()


def _write_catalog_entry(
    project_path: Path,
    *,
    media_type: str,
    filename_stem: str,
    label: str,
    index: int,
    extra: dict | None = None,
) -> tuple[Path, Path, dict]:
    label_dir = catalog_item_dir(str(project_path), media_type, filename_stem, label)
    label_dir.mkdir(parents=True, exist_ok=True)
    png_path = label_dir / f"object_{index:04d}.png"
    json_path = label_dir / f"object_{index:04d}.json"
    original_png = _write_png(png_path)
    meta = {
        "schema_version": "1",
        "media_type": media_type,
        "filename": f"{filename_stem}.mp4",
        "filename_stem": filename_stem,
        "media_id": "tmdb_1",
        "shot_id": f"tmdb_1@f{index:06d}-f{index + 12:06d}",
        "frame": index * 12,
        "label": label,
        "field": "objects",
        "confidence": 0.75,
        "bbox": [3, 3, 6, 6],
        "mask_area": 36,
        "frame_size": [12, 12],
        "png": png_path.name,
        "source_note": f"keep-{label}-{index}",
    }
    if extra:
        meta.update(extra)
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return json_path, png_path, meta | {"_png_bytes": original_png}


def _audit_row(
    *,
    json_path: Path,
    png_path: Path,
    label: str,
    classification: str,
    annotation_values: list[str],
    matched_words: list[str],
    missing_words: list[str],
    support_values: dict[str, list[str]],
    field: str = "objects",
    media_type: str = "movie",
    media_id: str = "tmdb_1",
    shot_id: str = "tmdb_1@f000000-f000012",
    frame: int = 0,
    timestamp: str = "2026-08-24T00:00:00+00:00",
    source_frame: str = "media/frames/best/movies/film/frame_0001.jpg",
) -> dict[str, str]:
    return {
        "media_type": media_type,
        "media_id": media_id,
        "shot_id": shot_id,
        "frame": str(frame),
        "field": field,
        "historical_label": label,
        "word_count": str(len(label.split())),
        "classification": classification,
        "annotation_values": json.dumps(annotation_values, ensure_ascii=False),
        "annotation_value_count": str(len(annotation_values)),
        "exact_annotation_match": str(classification == "VALID_EXACT" or classification == "VALID_SINGLE"),
        "all_words_present": str(classification == "VALID_EXACT"),
        "all_words_present_as_one_value": str(classification == "VALID_EXACT"),
        "separate_component_values": str(classification.startswith("QUESTIONABLE_")),
        "matched_words": json.dumps(matched_words, ensure_ascii=False),
        "missing_words": json.dumps(missing_words, ensure_ascii=False),
        "support_values": json.dumps(support_values, ensure_ascii=False),
        "reason": classification.lower(),
        "archive_json_path": str(json_path),
        "archive_png_path": str(png_path),
        "timestamp": timestamp,
        "source_frame": source_frame,
    }


def _write_audit(project_path: Path, rows: list[dict[str, str]]) -> Path:
    audit_dir = project_path / "outputs" / "tests" / "silhouette-semantic-audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "media_type", "media_id", "shot_id", "frame", "field", "historical_label",
        "word_count", "classification", "annotation_values", "annotation_value_count",
        "exact_annotation_match", "all_words_present", "all_words_present_as_one_value",
        "separate_component_values", "matched_words", "missing_words", "support_values",
        "reason", "archive_json_path", "archive_png_path", "timestamp", "source_frame",
    ]

    provenance_csv = audit_dir / "silhouette_provenance.csv"
    with provenance_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    classification = {
        "total_silhouette_records": len(rows),
        "valid_single": sum(1 for row in rows if row["classification"] == "VALID_SINGLE"),
        "valid_exact": sum(1 for row in rows if row["classification"] == "VALID_EXACT"),
        "questionable_split": sum(1 for row in rows if row["classification"] == "QUESTIONABLE_SPLIT"),
        "questionable_partial": sum(1 for row in rows if row["classification"] == "QUESTIONABLE_PARTIAL"),
        "questionable_unsupported": sum(1 for row in rows if row["classification"] == "QUESTIONABLE_UNSUPPORTED"),
        "unverifiable": sum(1 for row in rows if row["classification"] == "UNVERIFIABLE"),
    }
    report = {
        "classification": classification,
        "archive_root": str(project_path / "data" / "silhouettes" / "catalog" / "movie"),
    }
    (audit_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return audit_dir


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_default_audit_dir_prefers_outputs_tests(tmp_path):
    legacy_dir = tmp_path / "output" / "test" / "silhouette-semantic-audit"
    canonical_dir = tmp_path / "outputs" / "tests" / "silhouette-semantic-audit"
    legacy_dir.mkdir(parents=True)
    canonical_dir.mkdir(parents=True)

    assert default_audit_dir(tmp_path) == canonical_dir
    assert default_audit_dir(tmp_path) != legacy_dir


def test_provenance_cli_defaults_to_write_and_rejects_write_flag(tmp_path, capsys):
    args = cli.build_parser().parse_args(["index", "silhouette", "provenance"])
    assert args.dry_run is False

    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["index", "silhouette", "provenance", "--write"])
    assert excinfo.value.code == 2

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["index", "silhouette", "provenance", "-h"])
    help_text = capsys.readouterr().out
    assert "--dry-run" in help_text
    assert "--write" not in help_text
    assert "outputs/tests/silhouette-semantic-audit" in help_text


@pytest.mark.parametrize("dry_run", [False, True])
def test_provenance_cli_dispatch_uses_dry_run_flag(tmp_path, dry_run, capsys):
    report = {
        "total_records_examined": 2,
        "valid": 1,
        "questionable": 1,
        "already_classified": 0,
        "unmatched_audit_records": 0,
        "errors": 0,
        "safe_to_apply": True,
        "applied": not dry_run,
        "index_result": {"status": "ready", "count": 2},
    }
    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.silhouette_provenance.migrate_search_provenance", return_value=report) as migrate,
    ):
        cli._silhouette_provenance(Namespace(media="movie", audit_dir=None, dry_run=dry_run))

    assert migrate.call_args.kwargs["project_path"] == str(tmp_path)
    assert migrate.call_args.kwargs["dry_run"] is dry_run
    output = capsys.readouterr().out
    if dry_run:
        assert output.startswith("Provenance migration (dry-run):")
        assert "(dry-run)" in output
    else:
        assert output.startswith("Provenance migration:")
        assert "(dry-run)" not in output


def test_dry_run_and_real_migration_share_classification_counts_and_only_persistence_differs(tmp_path):
    project = tmp_path
    json_path, png_path, original = _write_catalog_entry(
        project,
        media_type="movie",
        filename_stem="film",
        label="coat",
        index=1,
        extra={"custom_field": "preserve-me"},
    )
    audit_dir = _write_audit(project, [
        _audit_row(
            json_path=json_path,
            png_path=png_path,
            label="coat",
            classification="VALID_SINGLE",
            annotation_values=["coat"],
            matched_words=["coat"],
            missing_words=[],
            support_values={"coat": ["coat"]},
            frame=original["frame"],
            shot_id=original["shot_id"],
            media_type=original["media_type"],
            media_id=original["media_id"],
        )
    ])

    dry_run = migrate_search_provenance(project, audit_dir=audit_dir, dry_run=True)
    assert dry_run["applied"] is False
    assert "search_provenance" not in _read_json(json_path)
    assert png_path.read_bytes() == original["_png_bytes"]

    real = migrate_search_provenance(project, audit_dir=audit_dir, dry_run=False)

    for key in ("total_records_examined", "valid", "questionable", "unmatched_audit_records", "errors"):
        assert dry_run[key] == real[key]
    assert dry_run["applied"] is False
    assert real["applied"] is True
    after = _read_json(json_path)
    assert after["search_provenance"]["state"] == "valid"
    assert after["search_provenance"]["reason"] == "single_word_label"
    assert png_path.read_bytes() == original["_png_bytes"]


@pytest.mark.parametrize(
    ("label", "classification", "annotation_values", "matched_words", "missing_words", "support_values", "expected_state", "expected_reason"),
    [
        ("coat", "VALID_SINGLE", ["coat"], ["coat"], [], {"coat": ["coat"]}, "valid", "single_word_label"),
        ("yellow coat", "VALID_EXACT", ["yellow coat"], ["yellow", "coat"], [], {"yellow": ["yellow coat"], "coat": ["yellow coat"]}, "valid", "exact_annotation_value"),
        ("yellow coat", "QUESTIONABLE_SPLIT", ["yellow", "coat"], ["yellow", "coat"], [], {"yellow": ["yellow"], "coat": ["coat"]}, "questionable", "multi_word_not_exact_annotation_value"),
        ("dark brown leather coat", "VALID_EXACT", ["dark brown leather coat"], ["dark", "brown", "leather", "coat"], [], {"dark": ["dark brown leather coat"]}, "valid", "exact_annotation_value"),
        ("dark brown leather coat", "QUESTIONABLE_SPLIT", ["dark brown", "leather", "coat"], ["dark", "brown", "leather", "coat"], [], {"dark": ["dark brown"], "leather": ["leather"], "coat": ["coat"]}, "questionable", "multi_word_not_exact_annotation_value"),
        ("black neckerchief", "QUESTIONABLE_PARTIAL", ["neckerchief"], ["neckerchief"], ["black"], {"black": [], "neckerchief": ["yellow neckerchief"]}, "questionable", "multi_word_not_exact_annotation_value"),
        ("brown leather riding coat", "VALID_EXACT", ["brown leather riding coat"], ["brown", "leather", "riding", "coat"], [], {"brown": ["brown leather riding coat"]}, "valid", "exact_annotation_value"),
        ("brown leather riding coat", "QUESTIONABLE_UNSUPPORTED", ["riding"], [], ["brown", "leather", "coat"], {"brown": [], "leather": [], "coat": []}, "questionable", "multi_word_not_exact_annotation_value"),
    ],
)
def test_migrate_search_provenance_maps_audit_rows_and_preserves_json_fields(
    tmp_path,
    label,
    classification,
    annotation_values,
    matched_words,
    missing_words,
    support_values,
    expected_state,
    expected_reason,
):
    project = tmp_path
    records = []
    json_path, png_path, original = _write_catalog_entry(
        project,
        media_type="movie",
        filename_stem="film",
        label=label,
        index=len(label),
        extra={"custom_field": "preserve-me"},
    )
    records.append(_audit_row(
        json_path=json_path,
        png_path=png_path,
        label=label,
        classification=classification,
        annotation_values=annotation_values,
        matched_words=matched_words,
        missing_words=missing_words,
        support_values=support_values,
        frame=original["frame"],
        shot_id=original["shot_id"],
        media_type=original["media_type"],
        media_id=original["media_id"],
    ))
    audit_dir = _write_audit(project, records)

    dry_run = migrate_search_provenance(project, audit_dir=audit_dir, dry_run=True)
    assert dry_run["safe_to_apply"] is True
    assert dry_run["applied"] is False
    assert dry_run["total_records_examined"] == 1
    assert dry_run["valid"] == (1 if expected_state == "valid" else 0)
    assert dry_run["questionable"] == (1 if expected_state == "questionable" else 0)
    assert dry_run["already_classified"] == 0
    assert dry_run["unmatched_audit_records"] == 0
    assert dry_run["errors"] == 0

    result = migrate_search_provenance(project, audit_dir=audit_dir, dry_run=False)
    assert result["applied"] is True
    assert result["errors"] == 0
    assert result["total_records_examined"] == 1
    assert result["valid"] == (1 if expected_state == "valid" else 0)
    assert result["questionable"] == (1 if expected_state == "questionable" else 0)
    assert result["already_classified"] in (0, 1)

    after = _read_json(json_path)
    assert after["custom_field"] == "preserve-me"
    assert after["label"] == label
    assert after["search_provenance"]["state"] == expected_state
    assert after["search_provenance"]["reason"] == expected_reason
    assert after["search_provenance"]["audit_version"] == SEARCH_PROVENANCE_AUDIT_VERSION
    assert after["search_provenance"]["annotation_values"] == annotation_values
    assert after["search_provenance"]["matched_words"] == matched_words

    before_png = original.pop("_png_bytes")
    assert png_path.read_bytes() == before_png
    assert all(after[key] == value for key, value in original.items() if key != "search_provenance")


def test_migrate_search_provenance_is_idempotent(tmp_path):
    project = tmp_path
    json_path, png_path, original = _write_catalog_entry(
        project,
        media_type="movie",
        filename_stem="film",
        label="coat",
        index=1,
    )
    audit_dir = _write_audit(project, [
        _audit_row(
            json_path=json_path,
            png_path=png_path,
            label="coat",
            classification="VALID_SINGLE",
            annotation_values=["coat"],
            matched_words=["coat"],
            missing_words=[],
            support_values={"coat": ["coat"]},
            frame=original["frame"],
            shot_id=original["shot_id"],
            media_type=original["media_type"],
            media_id=original["media_id"],
        )
    ])

    first = migrate_search_provenance(project, audit_dir=audit_dir, dry_run=False)
    second = migrate_search_provenance(project, audit_dir=audit_dir, dry_run=False)

    assert first["applied"] is True
    assert second["applied"] is True
    assert second["already_classified"] == 1
    assert second["updated"] == 0
    assert _read_json(json_path)["search_provenance"]["state"] == "valid"
    assert png_path.read_bytes() == original["_png_bytes"]


def test_migrate_search_provenance_rejects_unmatched_or_unverifiable_rows(tmp_path):
    project = tmp_path
    json_path, png_path, original = _write_catalog_entry(
        project,
        media_type="movie",
        filename_stem="film",
        label="coat",
        index=1,
    )
    audit_dir = _write_audit(project, [
        _audit_row(
            json_path=project / "data" / "silhouettes" / "catalog" / "movie" / "missing" / "object_0001.json",
            png_path=project / "data" / "silhouettes" / "catalog" / "movie" / "missing" / "object_0001.png",
            label="coat",
            classification="VALID_SINGLE",
            annotation_values=["coat"],
            matched_words=["coat"],
            missing_words=[],
            support_values={"coat": ["coat"]},
        ),
        _audit_row(
            json_path=json_path,
            png_path=png_path,
            label="coat",
            classification="UNVERIFIABLE",
            annotation_values=[],
            matched_words=[],
            missing_words=["coat"],
            support_values={"coat": []},
            frame=original["frame"],
            shot_id=original["shot_id"],
            media_type=original["media_type"],
            media_id=original["media_id"],
        ),
    ])

    report = migrate_search_provenance(project, audit_dir=audit_dir, dry_run=False)

    assert report["safe_to_apply"] is False
    assert report["applied"] is False
    assert report["errors"] >= 2
    assert report["unmatched_audit_records"] == 1
    assert "search_provenance" not in _read_json(json_path)
    assert png_path.read_bytes() == original["_png_bytes"]