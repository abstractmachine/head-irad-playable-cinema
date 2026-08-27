from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from services.silhouette_number_audit import (
    QUESTIONABLE_NUMBER,
    QUESTIONABLE_PARTIAL,
    QUESTIONABLE_SPLIT,
    QUESTIONABLE_UNSUPPORTED,
    UNVERIFIABLE,
    VALID_CLASSIFICATION,
    _canonical_number_token,
    audit_silhouette_number_provenance,
)


def _write_png(path: Path) -> None:
    image = Image.new("RGBA", (12, 12), (0, 0, 0, 0))
    for y in range(3, 9):
        for x in range(3, 9):
            image.putpixel((x, y), (255, 255, 255, 255))
    image.save(path)


def _write_catalog_entry(
    project_path: Path,
    *,
    media_type: str,
    filename_stem: str,
    label: str,
    index: int,
    field: str = "objects",
    extra: dict | None = None,
) -> dict:
    label_dir = project_path / "data" / "silhouettes" / "catalog" / media_type / filename_stem / label.replace(" ", "_")
    label_dir.mkdir(parents=True, exist_ok=True)
    png_path = label_dir / f"object_{index:04d}.png"
    json_path = label_dir / f"object_{index:04d}.json"
    _write_png(png_path)

    payload = {
        "schema_version": "1",
        "media_type": media_type,
        "filename": f"{filename_stem}.mp4",
        "filename_stem": filename_stem,
        "media_id": "tmdb_1",
        "shot_id": f"tmdb_1@f{index:06d}-f{index + 12:06d}",
        "frame": index * 12,
        "label": label,
        "field": field,
        "confidence": 0.75,
        "bbox": [3, 3, 6, 6],
        "mask_area": 36,
        "frame_size": [12, 12],
        "png": png_path.name,
        "source_frame": f"media/frames/best/{media_type}s/{filename_stem}/frame_{index:04d}.jpg",
        "timestamp": "2026-08-24T00:00:00+00:00",
    }
    if extra:
        payload.update(extra)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload | {"json_path": json_path, "png_path": png_path}


def _write_annotation_file(project_path: Path, filename: str, media_type: str, entries: list[dict]) -> Path:
    path = project_path / "data" / "annotations" / "shots" / media_type / f"{Path(filename).stem}.annotations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def _make_entry(shot_id: str, annotation: dict) -> dict:
    return {"shot": {"shot_id": shot_id, "annotation": annotation}}


def test_conservative_number_normalization_keeps_ambiguous_plurals_literal():
    surface_forms = {"band", "pants", "glass", "glove", "plank", "coat"}

    assert _canonical_number_token("planks", surface_forms) == "plank"
    assert _canonical_number_token("gloves", surface_forms) == "glove"
    assert _canonical_number_token("coats", surface_forms) == "coat"
    assert _canonical_number_token("pants", surface_forms) == "pants"
    assert _canonical_number_token("glasses", surface_forms) == "glasses"


def test_number_audit_classifies_number_split_partial_and_unverifiable(tmp_path):
    project = tmp_path

    # number ambiguity
    number_a = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="arm band", index=1)
    number_b = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="wooden plank", index=2)
    number_c = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="black glove", index=3)

    # exact valid
    valid_exact = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="yellow coat", index=4)

    # split / partial / unsupported / unverifiable
    split = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="black neckerchief", index=5, field="wearing")
    partial_arm = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="arm band", index=6)
    partial_band = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="arm band", index=7)
    unsupported = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="arm band", index=8)
    missing = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="white shirt", index=9)

    shot_entries = [
        _make_entry(number_a["shot_id"], {"objects": ["arm bands"]}),
        _make_entry(number_b["shot_id"], {"objects": ["wooden planks"]}),
        _make_entry(number_c["shot_id"], {"objects": ["black gloves"]}),
        _make_entry(valid_exact["shot_id"], {"objects": ["yellow coat"]}),
        _make_entry(split["shot_id"], {"wearing": ["black gloves", "green neckerchief"]}),
        _make_entry(partial_arm["shot_id"], {"objects": ["arm"]}),
        _make_entry(partial_band["shot_id"], {"objects": ["band"]}),
        _make_entry(unsupported["shot_id"], {"objects": []}),
    ]
    _write_annotation_file(project, "Film.mp4", "movie", shot_entries)

    report = audit_silhouette_number_provenance(project, media_type="movie")

    assert report["archive_modified"] is False
    assert report["classification"]["valid"] == 1
    assert report["classification"]["questionable_number"] == 3
    assert report["classification"]["questionable_split"] == 1
    assert report["classification"]["questionable_partial"] == 2
    assert report["classification"]["questionable_unsupported"] == 1
    assert report["classification"]["unverifiable"] == 1

    provenance_rows = (project / "outputs" / "tests" / "silhouette-number-audit" / "silhouette_number_provenance.csv").read_text(encoding="utf-8")
    assert "QUESTIONABLE_NUMBER" in provenance_rows
    assert "QUESTIONABLE_SPLIT" in provenance_rows
    assert "QUESTIONABLE_PARTIAL" in provenance_rows
    assert "QUESTIONABLE_UNSUPPORTED" in provenance_rows
    assert "UNVERIFIABLE" in provenance_rows
    assert "VALID" in provenance_rows

    safety = report["safety"]
    assert safety["archive_same_before_after"] is True
    assert safety["annotations_same_before_after"] is True
    assert safety["indexes_same_before_after"] is True
    assert safety["metadata_same_before_after"] is True
    assert safety["palettes_same_before_after"] is True
    assert safety["shotlists_same_before_after"] is True


def test_number_audit_preserves_single_word_validity(tmp_path):
    project = tmp_path

    record = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="coat", index=1)
    _write_annotation_file(project, "Film.mp4", "movie", [_make_entry(record["shot_id"], {"objects": ["coat", "shirts"]})])

    report = audit_silhouette_number_provenance(project, media_type="movie")

    assert report["classification"]["valid"] == 1
    assert report["classification"]["valid_single"] == 1
    assert report["classification"]["valid_exact"] == 0
    assert report["classification"]["questionable_number"] == 0


def test_number_audit_writes_reports_into_canonical_output_dir(tmp_path):
    project = tmp_path

    record = _write_catalog_entry(project, media_type="movie", filename_stem="film", label="yellow coat", index=1)
    _write_annotation_file(project, "Film.mp4", "movie", [_make_entry(record["shot_id"], {"objects": ["yellow coat"]})])

    report = audit_silhouette_number_provenance(project, media_type="movie")
    audit_dir = project / "outputs" / "tests" / "silhouette-number-audit"

    assert Path(report["report_json"]).parent == audit_dir
    assert Path(report["report_md"]).parent == audit_dir
    assert Path(report["summary_csv"]).parent == audit_dir
    assert Path(report["provenance_csv"]).parent == audit_dir
    assert (audit_dir / "report.json").exists()
    assert (audit_dir / "report.md").exists()
    assert (audit_dir / "silhouette_number_provenance.csv").exists()
    assert (audit_dir / "label_summary.csv").exists()
