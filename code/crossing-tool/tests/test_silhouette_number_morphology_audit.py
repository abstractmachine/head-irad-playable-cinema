from __future__ import annotations

import csv
import json
from pathlib import Path

import inflect

from services.silhouette_number_morphology_audit import (
    EXACT,
    MORPHOLOGY_UNRESOLVED,
    NOT_NUMBER_VARIANT,
    NUMBER_SEMANTICALLY_AMBIGUOUS,
    NUMBER_VARIANT,
    PHASE1_EXCEPTION_WORDS,
    PHASE1_REQUIRED_PAIRS,
    audit_silhouette_number_morphology,
    default_output_dir,
    default_source_audit_dir,
    _best_candidate,
    _pair_relation,
    _phrase_comparison,
)


def _write_source_audit(project_path: Path, rows: list[dict[str, str]], report: dict | None = None) -> Path:
    audit_dir = project_path / "outputs" / "tests" / "silhouette-number-audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "media_type", "media_id", "shot_id", "frame", "field", "historical_label",
        "word_count", "classification", "annotation_values", "annotation_value_count",
        "exact_annotation_match", "all_words_present", "all_words_present_as_one_value",
        "separate_component_values", "matched_words", "missing_words", "support_values",
        "number_support_values", "reason", "archive_json_path", "archive_png_path",
        "timestamp", "source_frame",
    ]
    with (audit_dir / "silhouette_number_provenance.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    if report is None:
        report = {
            "classification": {"questionable_number": len(rows)},
            "archive_root": str(project_path / "data" / "silhouettes" / "catalog" / "movie"),
        }
    (audit_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return audit_dir


def _row(
    *,
    label: str,
    classification: str,
    annotation_values: list[str],
    field: str = "objects",
    shot_id: str = "tmdb_1@f000000-f000012",
    media_type: str = "movie",
    media_id: str = "tmdb_1",
    frame: int = 0,
    annotation_value_count: int | None = None,
    json_path: str = "/tmp/object_0001.json",
    png_path: str = "/tmp/object_0001.png",
) -> dict[str, str]:
    if annotation_value_count is None:
        annotation_value_count = len(annotation_values)
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
        "annotation_value_count": str(annotation_value_count),
        "exact_annotation_match": "False",
        "all_words_present": "False",
        "all_words_present_as_one_value": "False",
        "separate_component_values": "False",
        "matched_words": json.dumps([], ensure_ascii=False),
        "missing_words": json.dumps([], ensure_ascii=False),
        "support_values": json.dumps({}, ensure_ascii=False),
        "number_support_values": json.dumps([], ensure_ascii=False),
        "reason": classification.lower(),
        "archive_json_path": json_path,
        "archive_png_path": png_path,
        "timestamp": "2026-08-24T00:00:00+00:00",
        "source_frame": "media/frames/best/movies/film/frame_0001.jpg",
    }


def test_inflect_probe_matches_required_pairs_and_exception_words():
    engine = inflect.engine()

    for left, right in PHASE1_REQUIRED_PAIRS:
        relation = _pair_relation(left, right, engine)
        assert relation["compare_nouns"] in {"eq", "s:p", "p:s"}

    for word in PHASE1_EXCEPTION_WORDS:
        relation = _pair_relation(word.rstrip("s"), word, engine)
        assert relation["compare_nouns"] in {"s:p", "p:s", "eq"}


def test_phrase_comparison_keeps_positive_number_variants():
    engine = inflect.engine()

    number_variant = _phrase_comparison("arm band", "arm bands", engine)
    assert number_variant["classification"] == NUMBER_VARIANT
    assert number_variant["direction"] == "silhouette_singular_annotation_plural"

    black_glove = _phrase_comparison("black glove", "black gloves", engine)
    assert black_glove["classification"] == NUMBER_VARIANT

    wooden_plank = _phrase_comparison("wooden plank", "wooden planks", engine)
    assert wooden_plank["classification"] == NUMBER_VARIANT


def test_phrase_comparison_keeps_explicit_rejections_as_not_number_variant():
    engine = inflect.engine()

    negative_count = _phrase_comparison("yellow coat", "coat", engine)
    assert negative_count["classification"] == NOT_NUMBER_VARIANT

    black_neckerchief = _phrase_comparison("black neckerchief", "black gloves", engine)
    assert black_neckerchief["classification"] == NOT_NUMBER_VARIANT
    assert black_neckerchief["morphology_library_result"]["pairs"][0]["kind"] == NOT_NUMBER_VARIANT

    negative_semantic = _phrase_comparison("black glove", "brown gloves", engine)
    assert negative_semantic["classification"] == NOT_NUMBER_VARIANT

    wagon_wheel = _phrase_comparison("wagon wheel", "wooden wheels", engine)
    assert wagon_wheel["classification"] == NOT_NUMBER_VARIANT

    yellow_coat = _phrase_comparison("yellow coat", "yellow coats", engine)
    assert yellow_coat["classification"] == NUMBER_VARIANT


def test_phrase_comparison_marks_p_plural_pairs_as_unresolved():
    engine = inflect.engine()

    unresolved = _phrase_comparison("cacti", "cactuses", engine)
    assert unresolved["classification"] == MORPHOLOGY_UNRESOLVED
    assert unresolved["morphology_library_result"]["pairs"][0]["compare_nouns"] == "p:p"


def test_semantic_exception_words_remain_ambiguous():
    engine = inflect.engine()

    for singular, plural in [
        ("pant", "pants"),
        ("trouser", "trousers"),
        ("scissor", "scissors"),
        ("clothe", "clothes"),
    ]:
        semantic = _phrase_comparison(singular, plural, engine)
        assert semantic["classification"] == NUMBER_SEMANTICALLY_AMBIGUOUS


def test_best_candidate_prefers_exact_then_number_variant_then_controls():
    engine = inflect.engine()
    selected = _best_candidate("arm band", ["arm bands", "arm band"], engine)
    assert selected["classification"] == EXACT

    selected = _best_candidate("arm band", ["arm bands", "arm"], engine)
    assert selected["classification"] == NUMBER_VARIANT


def test_audit_writes_canonical_morphology_outputs(tmp_path):
    project = tmp_path
    rows = [
        _row(label="arm band", classification="QUESTIONABLE_NUMBER", annotation_values=["arm bands"], json_path=str(project / "data" / "silhouettes" / "catalog" / "movie" / "film" / "arm_band" / "object_0001.json"), png_path=str(project / "data" / "silhouettes" / "catalog" / "movie" / "film" / "arm_band" / "object_0001.png")),
        _row(label="yellow coat", classification="QUESTIONABLE_PARTIAL", annotation_values=["coat"], json_path=str(project / "data" / "silhouettes" / "catalog" / "movie" / "film" / "yellow_coat" / "object_0002.json"), png_path=str(project / "data" / "silhouettes" / "catalog" / "movie" / "film" / "yellow_coat" / "object_0002.png")),
    ]
    source_dir = _write_source_audit(project, rows, report={"classification": {"questionable_number": 1}, "archive_root": str(project / "data" / "silhouettes" / "catalog" / "movie")})

    report = audit_silhouette_number_morphology(project, source_audit_dir=source_dir)

    output_dir = default_output_dir(project)
    assert Path(report["artifacts"]["report_json"]).parent == output_dir
    assert Path(report["artifacts"]["report_md"]).parent == output_dir
    assert Path(report["artifacts"]["morphology_records_csv"]).parent == output_dir
    assert Path(report["artifacts"]["morphology_labels_csv"]).parent == output_dir
    assert report["phase1"]["assessment"] == "YES WITH EXCEPTIONS"
    assert report["classification"]["number_variant"] == 1


def test_audit_rejects_non_number_rows_as_inputs(tmp_path):
    project = tmp_path
    rows = [
        _row(label="arm band", classification="QUESTIONABLE_SPLIT", annotation_values=["arm bands"]),
    ]
    source_dir = _write_source_audit(project, rows, report={"classification": {"questionable_number": 0}, "archive_root": str(project / "data" / "silhouettes" / "catalog" / "movie")})

    report = audit_silhouette_number_morphology(project, source_audit_dir=source_dir)

    assert report["source_audit"]["questionable_number_records_examined"] == 0
    assert report["classification"]["number_variant"] == 0
