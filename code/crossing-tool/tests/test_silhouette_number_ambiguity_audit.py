from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import cli
from services.silhouette_number_ambiguity_audit import (
    INFLECT_LIMITATION,
    LEXICAL_NUMBER_ASYMMETRY,
    OTHER,
    PLURALIA_TANTUM,
    audit_silhouette_number_ambiguity,
    default_output_dir,
)
from services.silhouette_number_morphology_audit import NUMBER_SEMANTICALLY_AMBIGUOUS


SOURCE_FIELDNAMES = [
    "media_type",
    "media_id",
    "shot_id",
    "frame",
    "field",
    "annotation_value",
    "annotation_values",
    "silhouette_label",
    "relationship",
    "direction",
    "morphology_result",
    "archive_json_path",
    "archive_png_path",
]


def _write_png(path: Path, color: tuple[int, int, int, int], size: tuple[int, int] = (80, 50)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, color).save(path)


def _write_catalog_object(
    project: Path,
    *,
    media_id: str,
    shot_id: str,
    frame: int,
    field: str,
    label: str,
    annotation_value: str,
    object_index: int,
) -> tuple[Path, Path]:
    source_frame = project / "media" / "frames" / "best" / "movie" / "film_a" / f"frame_{frame:06d}.png"
    _write_png(source_frame, (200, 180, 150, 255), size=(160, 90))
    label_dir = project / "data" / "silhouettes" / "catalog" / "movie" / "film_a" / label.replace(" ", "_")
    label_dir.mkdir(parents=True, exist_ok=True)
    json_path = label_dir / f"object_{object_index:04d}.json"
    png_path = label_dir / f"object_{object_index:04d}.png"
    _write_png(png_path, (40 + object_index * 20, 80, 140, 255))
    json_path.write_text(
        json.dumps(
            {
                "media_type": "movie",
                "media_id": media_id,
                "shot_id": shot_id,
                "frame": frame,
                "field": field,
                "label": label,
                "source_frame": str(source_frame),
                "png": png_path.name,
                "search_provenance": {
                    "state": "questionable",
                    "reason": "multi_word_not_exact_annotation_value",
                    "annotation_value": annotation_value,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return json_path, png_path


def _source_row(
    *,
    media_id: str,
    shot_id: str,
    frame: int,
    field: str,
    annotation_value: str,
    silhouette_label: str,
    relationship: str,
    archive_json_path: Path,
    archive_png_path: Path,
) -> dict[str, str]:
    return {
        "media_type": "movie",
        "media_id": media_id,
        "shot_id": shot_id,
        "frame": str(frame),
        "field": field,
        "annotation_value": annotation_value,
        "annotation_values": json.dumps([annotation_value]),
        "silhouette_label": silhouette_label,
        "relationship": relationship,
        "direction": "silhouette_singular_annotation_plural",
        "morphology_result": json.dumps(
            {
                "classification": relationship,
                "reason": "number relation involves a semantic exception",
                "morphology_library_result": {"compare_nouns": ["s:p"]},
            }
        ),
        "archive_json_path": str(archive_json_path),
        "archive_png_path": str(archive_png_path),
    }


def _write_preference_audit(project: Path, rows: list[dict[str, str]]) -> Path:
    audit_dir = project / "outputs" / "tests" / "silhouette-number-preference-audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    ambiguous_count = sum(row["relationship"] == NUMBER_SEMANTICALLY_AMBIGUOUS for row in rows)
    (audit_dir / "report.json").write_text(
        json.dumps(
            {
                "counts": {
                    "relationship": {
                        NUMBER_SEMANTICALLY_AMBIGUOUS: ambiguous_count,
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with (audit_dir / "relationships.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SOURCE_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return audit_dir


def test_ambiguity_audit_selects_exact_rows_groups_families_and_renders_examples(tmp_path):
    rows: list[dict[str, str]] = []
    fixtures = [
        ("objects", "glasses", "glass", NUMBER_SEMANTICALLY_AMBIGUOUS),
        ("objects", "glasses", "glass", NUMBER_SEMANTICALLY_AMBIGUOUS),
        ("objects", "pants", "pant", NUMBER_SEMANTICALLY_AMBIGUOUS),
        ("objects", "shot glass", "shot glasses", NUMBER_SEMANTICALLY_AMBIGUOUS),
        ("wearing", "pants", "pant", NUMBER_SEMANTICALLY_AMBIGUOUS),
        ("objects", "coat", "coats", "NUMBER_VARIANT_NON_PREFERRED"),
    ]
    fixtures.extend(
        [("objects", "glasses", "glass", NUMBER_SEMANTICALLY_AMBIGUOUS)] * 20
    )
    for index, (field, annotation, label, relationship) in enumerate(fixtures, 1):
        json_path, png_path = _write_catalog_object(
            tmp_path,
            media_id="tmdb_1",
            shot_id=f"tmdb_1@f{index:06d}-f{index + 1:06d}",
            frame=index,
            field=field,
            label=label,
            annotation_value=annotation,
            object_index=index,
        )
        rows.append(
            _source_row(
                media_id="tmdb_1",
                shot_id=f"tmdb_1@f{index:06d}-f{index + 1:06d}",
                frame=index,
                field=field,
                annotation_value=annotation,
                silhouette_label=label,
                relationship=relationship,
                archive_json_path=json_path,
                archive_png_path=png_path,
            )
        )
    source_dir = _write_preference_audit(tmp_path, rows)

    report = audit_silhouette_number_ambiguity(tmp_path, preference_audit_dir=source_dir)
    output_dir = default_output_dir(tmp_path)

    assert report["selection"]["selected_record_count"] == 25
    assert report["selection"]["selection_matches_source_report"] is True
    assert report["inventory"]["unique_relationships"] == 4
    assert report["forensic_category_counts"][LEXICAL_NUMBER_ASYMMETRY] == 22
    assert report["forensic_category_counts"][PLURALIA_TANTUM] == 1
    assert report["forensic_category_counts"][INFLECT_LIMITATION] == 1
    assert report["forensic_category_counts"][OTHER] == 1
    assert report["inventory"]["concentration"]["top_10"]["record_count"] == 25
    assert report["safety"]["implementation_source_writes"] is False
    assert all(report["safety"]["data_roots_unchanged"].values())

    relationships = list(csv.DictReader((output_dir / "relationships.csv").open(encoding="utf-8")))
    assert len(relationships) == 4
    wearing_row = next(row for row in relationships if row["field"] == "wearing")
    assert wearing_row["forensic_category"] == OTHER
    assert wearing_row["recommendation"] == "REVIEW_BEFORE_POLICY"
    assert "no fuzzy or cross-value policy" in wearing_row["interpretation_reason"]

    families = list(csv.DictReader((output_dir / "families.csv").open(encoding="utf-8")))
    assert len(families) == 4
    assert any(row["family"] == "pants / pant" and row["record_count"] == "1" for row in families)
    assert any(row["family"] == "wearing: pants / pant" for row in families)

    samples = list(csv.DictReader((output_dir / "sampled_examples.csv").open(encoding="utf-8")))
    assert len(samples) == 25
    for sample in samples:
        assert Path(sample["example_panel_path"]).exists()
        assert Path(sample["example_evidence_path"]).exists()
        assert Path(sample["source_frame_path"]).exists()
        assert Path(sample["silhouette_png_path"]).exists()


def test_ambiguity_audit_rejects_preference_count_mismatch(tmp_path):
    json_path, png_path = _write_catalog_object(
        tmp_path,
        media_id="tmdb_1",
        shot_id="tmdb_1@f000001-f000002",
        frame=1,
        field="objects",
        label="pant",
        annotation_value="pants",
        object_index=1,
    )
    source_dir = _write_preference_audit(
        tmp_path,
        [
            _source_row(
                media_id="tmdb_1",
                shot_id="tmdb_1@f000001-f000002",
                frame=1,
                field="objects",
                annotation_value="pants",
                silhouette_label="pant",
                relationship=NUMBER_SEMANTICALLY_AMBIGUOUS,
                archive_json_path=json_path,
                archive_png_path=png_path,
            )
        ],
    )
    report_path = source_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["counts"]["relationship"][NUMBER_SEMANTICALLY_AMBIGUOUS] = 2
    report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")

    try:
        audit_silhouette_number_ambiguity(tmp_path, preference_audit_dir=source_dir)
    except ValueError as exc:
        assert "Preference audit count mismatch" in str(exc)
    else:
        raise AssertionError("Expected exact source-count validation to fail")


def test_cli_number_ambiguity_audit_dispatch(monkeypatch, tmp_path, capsys):
    calls = []
    real_wrapper = cli._silhouette_number_ambiguity_audit
    monkeypatch.setattr(cli, "_silhouette_number_ambiguity_audit", lambda args: calls.append(args))
    cli._index_silhouette(SimpleNamespace(silhouette_action="number-ambiguity-audit"))
    assert len(calls) == 1
    monkeypatch.setattr(cli, "_silhouette_number_ambiguity_audit", real_wrapper)

    from services import silhouette_number_ambiguity_audit as audit_module

    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: str(tmp_path) if key == "path" else default)
    monkeypatch.setattr(
        audit_module,
        "audit_silhouette_number_ambiguity",
        lambda *args, **kwargs: {
            "inventory": {"total_records": 5, "unique_relationships": 4},
            "families": [{"family": "pants / pant"}],
            "policy_candidates": {"candidate_records": 1, "retain_ambiguous_records": 4},
            "artifacts": {
                "report_md": str(tmp_path / "report.md"),
                "report_json": str(tmp_path / "report.json"),
                "relationships_csv": str(tmp_path / "relationships.csv"),
                "families_csv": str(tmp_path / "families.csv"),
                "sampled_examples_csv": str(tmp_path / "sampled_examples.csv"),
                "examples_dir": str(tmp_path / "examples"),
            },
        },
    )
    cli._silhouette_number_ambiguity_audit(
        SimpleNamespace(preference_audit_dir=None, output_dir=None, sample_size=25)
    )
    output = capsys.readouterr().out
    assert "Ambiguity audit: records=5" in output
    assert "families=1" in output