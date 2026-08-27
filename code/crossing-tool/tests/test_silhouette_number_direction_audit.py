from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from PIL import Image

import cli
from services.silhouette_number_direction_audit import (
    FORWARD_DIRECTION,
    HISTORICAL_LABEL_PROPAGATION,
    MORPHOLOGICALLY_VALID_BUT_VISUALLY_ODD,
    REVERSE_DIRECTION,
    SEGMENTATION_MULTIPLICITY,
    VISUAL_PLURAL,
    audit_silhouette_number_direction,
    default_output_dir,
    default_source_audit_dir,
    _interpret_example,
)


MORPHOLOGY_FIELDNAMES = [
    "media_type",
    "media_id",
    "shot_id",
    "frame",
    "field",
    "silhouette_label",
    "annotation_value",
    "existing_number_classification",
    "new_classification",
    "direction",
    "silhouette_tokens",
    "annotation_tokens",
    "differing_token_positions",
    "singular_form",
    "plural_form",
    "morphology_library",
    "morphology_library_result",
    "morphologically_clean",
    "semantic_ambiguity",
    "reason",
    "archive_json_path",
    "archive_png_path",
    "timestamp",
    "source_frame",
]


def _write_png(path: Path, color: tuple[int, int, int, int], size: tuple[int, int] = (32, 32)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, color).save(path)


def _safe_label(label: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", label.lower().strip())


def _write_catalog_object(
    project_path: Path,
    *,
    media_id: str,
    shot_id: str,
    frame: int,
    label: str,
    field: str,
    filename_stem: str,
    object_index: int,
    source_frame: Path,
    color: tuple[int, int, int, int],
) -> tuple[Path, Path]:
    label_dir = project_path / "data" / "silhouettes" / "catalog" / "movie" / filename_stem / _safe_label(label)
    label_dir.mkdir(parents=True, exist_ok=True)
    json_path = label_dir / f"object_{object_index:04d}.json"
    png_path = label_dir / f"object_{object_index:04d}.png"
    _write_png(png_path, color)
    json_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "media_type": "movie",
                "filename": f"{filename_stem}.mp4",
                "filename_stem": filename_stem,
                "media_id": media_id,
                "shot_id": shot_id,
                "frame": frame,
                "label": label,
                "field": field,
                "confidence": 0.9,
                "confidence_broad": 0.8,
                "bbox": [4, 4, 16, 16],
                "mask_area": 256,
                "frame_size": [32, 32],
                "sam_model": "sam3",
                "frame_match_model": "clip-vit-base-patch32",
                "source_frame": str(source_frame),
                "png": png_path.name,
                "timestamp": "2026-08-24T00:00:00+00:00",
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return json_path, png_path


def _write_source_audit(project_path: Path, rows: list[dict[str, str]]) -> Path:
    audit_dir = project_path / "outputs" / "tests" / "silhouette-number-morphology-audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    with (audit_dir / "morphology_records.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MORPHOLOGY_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    (audit_dir / "report.json").write_text(
        json.dumps(
            {
                "classification": {"questionable_number": len(rows)},
                "archive_root": str(project_path / "data" / "silhouettes" / "catalog" / "movie"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return audit_dir


def _row(
    *,
    media_id: str,
    shot_id: str,
    frame: int,
    field: str,
    silhouette_label: str,
    annotation_value: str,
    direction: str,
    archive_json_path: Path,
    archive_png_path: Path,
    source_frame: Path,
    morphological_kind: str = "NUMBER_VARIANT",
) -> dict[str, str]:
    singular_form = annotation_value if direction == REVERSE_DIRECTION else silhouette_label
    plural_form = silhouette_label if direction == REVERSE_DIRECTION else annotation_value
    return {
        "media_type": "movie",
        "media_id": media_id,
        "shot_id": shot_id,
        "frame": str(frame),
        "field": field,
        "silhouette_label": silhouette_label,
        "annotation_value": annotation_value,
        "existing_number_classification": "QUESTIONABLE_NUMBER",
        "new_classification": morphological_kind,
        "direction": direction,
        "silhouette_tokens": json.dumps(silhouette_label.split(), ensure_ascii=False),
        "annotation_tokens": json.dumps(annotation_value.split(), ensure_ascii=False),
        "differing_token_positions": json.dumps([1], ensure_ascii=False),
        "singular_form": singular_form,
        "plural_form": plural_form,
        "morphology_library": "inflect",
        "morphology_library_result": json.dumps({"pairs": [{"compare_nouns": "s:p"}]}, ensure_ascii=False),
        "morphologically_clean": "True",
        "semantic_ambiguity": "False",
        "reason": "test",
        "archive_json_path": str(archive_json_path),
        "archive_png_path": str(archive_png_path),
        "timestamp": "2026-08-24T00:00:00+00:00",
        "source_frame": str(source_frame),
    }


def test_interpret_example_separates_reverse_and_forward_mechanisms():
    reverse_visual_plural = {
        "direction": REVERSE_DIRECTION,
        "silhouette_label": "stone walls",
        "annotation_value": "stone wall",
        "same_frame_number_variant_exists": True,
        "same_shot_number_variant_exists": True,
        "same_frame": True,
        "same_shot": True,
        "comparison": {"file_hash_equal": False, "pixel_equal_if_same_dimensions": False},
    }
    assert _interpret_example(reverse_visual_plural)[0] == VISUAL_PLURAL

    reverse_historical = {
        "direction": REVERSE_DIRECTION,
        "silhouette_label": "deer heads",
        "annotation_value": "deer head",
        "same_frame_number_variant_exists": False,
        "same_shot_number_variant_exists": True,
        "same_frame": False,
        "same_shot": True,
        "comparison": {"file_hash_equal": False, "pixel_equal_if_same_dimensions": False},
    }
    assert _interpret_example(reverse_historical)[0] == HISTORICAL_LABEL_PROPAGATION

    reverse_odd = {
        "direction": REVERSE_DIRECTION,
        "silhouette_label": "black gloves",
        "annotation_value": "black glove",
        "same_frame_number_variant_exists": False,
        "same_shot_number_variant_exists": False,
        "same_frame": False,
        "same_shot": False,
        "comparison": {"file_hash_equal": False, "pixel_equal_if_same_dimensions": False},
    }
    assert _interpret_example(reverse_odd)[0] == MORPHOLOGICALLY_VALID_BUT_VISUALLY_ODD

    forward_split = {
        "direction": FORWARD_DIRECTION,
        "silhouette_label": "wagon wheel",
        "annotation_value": "wagon wheels",
        "same_frame_number_variant_exists": True,
        "same_shot_number_variant_exists": True,
        "same_frame": True,
        "same_shot": True,
        "comparison": {"file_hash_equal": False, "pixel_equal_if_same_dimensions": False},
    }
    assert _interpret_example(forward_split)[0] == SEGMENTATION_MULTIPLICITY


def test_direction_audit_writes_read_only_outputs(tmp_path):
    project = tmp_path
    source_frame = project / "media" / "frames" / "best" / "movies" / "film_a" / "frame_000005.png"
    _write_png(source_frame, (220, 220, 220, 255), size=(320, 180))

    stone_wall_json, stone_wall_png = _write_catalog_object(
        project,
        media_id="tmdb_1",
        shot_id="tmdb_1@f000005-f000020",
        frame=5,
        label="stone wall",
        field="objects",
        filename_stem="film_a",
        object_index=1,
        source_frame=source_frame,
        color=(60, 120, 200, 255),
    )
    stone_walls_json, stone_walls_png = _write_catalog_object(
        project,
        media_id="tmdb_1",
        shot_id="tmdb_1@f000005-f000020",
        frame=5,
        label="stone walls",
        field="objects",
        filename_stem="film_a",
        object_index=2,
        source_frame=source_frame,
        color=(210, 120, 40, 255),
    )
    deer_head_json, deer_head_png = _write_catalog_object(
        project,
        media_id="tmdb_1",
        shot_id="tmdb_1@f000100-f000120",
        frame=112,
        label="deer head",
        field="objects",
        filename_stem="film_a",
        object_index=3,
        source_frame=source_frame,
        color=(120, 180, 70, 255),
    )
    deer_heads_json, deer_heads_png = _write_catalog_object(
        project,
        media_id="tmdb_1",
        shot_id="tmdb_1@f000100-f000120",
        frame=118,
        label="deer heads",
        field="objects",
        filename_stem="film_a",
        object_index=4,
        source_frame=source_frame,
        color=(170, 70, 130, 255),
    )
    wagon_wheel_json, wagon_wheel_png = _write_catalog_object(
        project,
        media_id="tmdb_1",
        shot_id="tmdb_1@f000200-f000240",
        frame=205,
        label="wagon wheel",
        field="objects",
        filename_stem="film_a",
        object_index=5,
        source_frame=source_frame,
        color=(40, 160, 160, 255),
    )
    wagon_wheels_json, wagon_wheels_png = _write_catalog_object(
        project,
        media_id="tmdb_1",
        shot_id="tmdb_1@f000200-f000240",
        frame=205,
        label="wagon wheels",
        field="objects",
        filename_stem="film_a",
        object_index=6,
        source_frame=source_frame,
        color=(180, 40, 180, 255),
    )

    source_dir = _write_source_audit(
        project,
        [
            _row(
                media_id="tmdb_1",
                shot_id="tmdb_1@f000005-f000020",
                frame=5,
                field="objects",
                silhouette_label="stone walls",
                annotation_value="stone wall",
                direction=REVERSE_DIRECTION,
                archive_json_path=stone_walls_json,
                archive_png_path=stone_walls_png,
                source_frame=source_frame,
            ),
            _row(
                media_id="tmdb_1",
                shot_id="tmdb_1@f000100-f000120",
                frame=118,
                field="objects",
                silhouette_label="deer heads",
                annotation_value="deer head",
                direction=REVERSE_DIRECTION,
                archive_json_path=deer_heads_json,
                archive_png_path=deer_heads_png,
                source_frame=source_frame,
            ),
            _row(
                media_id="tmdb_1",
                shot_id="tmdb_1@f000200-f000240",
                frame=205,
                field="objects",
                silhouette_label="wagon wheel",
                annotation_value="wagon wheels",
                direction=FORWARD_DIRECTION,
                archive_json_path=wagon_wheel_json,
                archive_png_path=wagon_wheel_png,
                source_frame=source_frame,
            ),
        ],
    )

    report = audit_silhouette_number_direction(project)

    output_dir = default_output_dir(project)
    assert Path(report["artifacts"]["report_json"]).parent == output_dir
    assert Path(report["artifacts"]["report_md"]).parent == output_dir
    assert Path(report["artifacts"]["reverse_relationships_csv"]).parent == output_dir
    assert Path(report["artifacts"]["sampled_examples_csv"]).parent == output_dir
    assert Path(report["artifacts"]["examples_dir"]).parent == output_dir
    assert report["source_inputs_same_before_after"] is True
    assert report["population"]["reverse_direction_records"] == 2
    assert report["population"]["forward_direction_records"] == 1
    assert report["by_field_reverse"]["objects"] == 2
    assert report["by_field_forward"]["objects"] == 1

    sampled_rows = list(csv.DictReader((output_dir / "sampled_examples.csv").open(encoding="utf-8")))
    assert len(sampled_rows) == 3
    interpretations = {row["forensic_interpretation"] for row in sampled_rows}
    assert interpretations == {
        VISUAL_PLURAL,
        HISTORICAL_LABEL_PROPAGATION,
        SEGMENTATION_MULTIPLICITY,
    }
    for row in sampled_rows:
        assert Path(row["example_panel_path"]).exists()
        assert Path(row["example_evidence_path"]).exists()
        assert Path(row["silhouette_json_path"]).exists()
        assert Path(row["silhouette_png_path"]).exists()

    assert default_source_audit_dir(project) == source_dir


class _Args:
    silhouette_action = "number-direction-audit"
    morphology_audit_dir = None
    output_dir = None


def test_index_silhouette_number_direction_audit_dispatch(monkeypatch, tmp_path):
    calls = {}

    def fake_audit(project_path, *, morphology_audit_dir=None, output_dir=None):
        calls["project_path"] = project_path
        calls["morphology_audit_dir"] = morphology_audit_dir
        calls["output_dir"] = output_dir
        return {
            "population": {
                "reverse_direction_records": 2,
                "forward_direction_records": 1,
            },
            "sample_counts": {
                "reverse_examples": 2,
                "forward_controls": 1,
            },
            "artifacts": {
                "report_md": str(tmp_path / "outputs" / "tests" / "silhouette-number-direction-audit" / "report.md"),
                "report_json": str(tmp_path / "outputs" / "tests" / "silhouette-number-direction-audit" / "report.json"),
                "reverse_relationships_csv": str(tmp_path / "outputs" / "tests" / "silhouette-number-direction-audit" / "reverse_relationships.csv"),
                "sampled_examples_csv": str(tmp_path / "outputs" / "tests" / "silhouette-number-direction-audit" / "sampled_examples.csv"),
                "examples_dir": str(tmp_path / "outputs" / "tests" / "silhouette-number-direction-audit" / "examples"),
            },
        }

    monkeypatch.setattr(cli, "_silhouette_number_direction_audit", lambda args: fake_audit(str(tmp_path), morphology_audit_dir=args.morphology_audit_dir, output_dir=args.output_dir))

    cli._index_silhouette(_Args())
    assert calls == {
        "project_path": str(tmp_path),
        "morphology_audit_dir": None,
        "output_dir": None,
    }