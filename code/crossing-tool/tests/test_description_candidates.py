import json

import services.search as search_mod
from services.description_candidates import (
    build_description_candidates,
    candidate_path,
    load_description_candidates,
)


def _write_annotations(project_path, annotations):
    directory = project_path / "data" / "annotations" / "shots" / "movie"
    directory.mkdir(parents=True)
    (directory / "Film.annotations.json").write_text(
        json.dumps([
            {"shot": {"annotation": annotation}}
            for annotation in annotations
        ]),
        encoding="utf-8",
    )


def test_description_candidates_are_separate_and_document_scoped(tmp_path):
    _write_annotations(tmp_path, [
        {"description": "Two horses stand beside a wagon. Horses wait.", "animals": ["horses"]},
        {"description": "A horse passes the wagon.", "animals": ["horse"]},
        {"description": "A rare lantern glows.", "objects": ["lantern"]},
    ])

    artifact = build_description_candidates(str(tmp_path), min_document_frequency=2)

    assert candidate_path(str(tmp_path)).name == "vocabulary-movie-derived.json"
    assert artifact["meta"]["family"] == "derived"
    assert artifact["fields"]["description"]["horse"] == {
        "count": 2,
        "document_frequency": 2,
        "aliases": ["horse", "horses"],
        "origin": "derived",
        "source_fields": ["description"],
        "normalization": "free_text_terms_v4",
        "quality": 0.5,
    }
    assert artifact["fields"]["description"]["wagon"]["document_frequency"] == 2
    assert "lantern" not in artifact["fields"]["description"]
    assert load_description_candidates(str(tmp_path)) == artifact


def test_description_candidates_do_not_mutate_annotations(tmp_path):
    _write_annotations(tmp_path, [
        {"description": "A saddle rests nearby."},
        {"description": "The saddle is lifted."},
    ])
    annotation_path = next(
        (tmp_path / "data" / "annotations" / "shots" / "movie").glob("*.json")
    )
    original = annotation_path.read_bytes()

    build_description_candidates(str(tmp_path))

    assert annotation_path.read_bytes() == original


def test_derived_vocabulary_includes_text_with_source_provenance(tmp_path):
    _write_annotations(tmp_path, [
        {"text": "Warning signs glow."},
        {"description": "A warning appears."},
    ])

    artifact = build_description_candidates(str(tmp_path))

    assert artifact["fields"]["description"]["warning"]["source_fields"] == [
        "description", "text"
    ]


def test_derived_vocabulary_includes_non_latin_description_terms(tmp_path):
    _write_annotations(tmp_path, [
        {"description": "勇禁 appears in the frame."},
        {"description": "The 勇禁 mark remains."},
    ])

    artifact = build_description_candidates(str(tmp_path))
    result = search_mod.vocabulary_from_index("description", str(tmp_path), sort="count")

    assert artifact["fields"]["description"]["勇禁"]["source_fields"] == ["description"]
    assert {item["value"] for item in result["items"]} >= {"勇禁"}


def test_derived_vocabulary_query_filters_to_the_selected_source_field(tmp_path):
    _write_annotations(tmp_path, [
        {"text": "Zealand signs glow."},
        {"text": "A Zealand poster hangs."},
        {"description": "Two wagons pass."},
        {"description": "A wagon waits."},
    ])
    build_description_candidates(str(tmp_path))

    description = search_mod.vocabulary_from_index(
        "description", str(tmp_path), sort="count"
    )
    text = search_mod.vocabulary_from_index("text", str(tmp_path), sort="count")

    assert [item["value"] for item in description["items"]] == ["wagon"]
    assert [item["value"] for item in text["items"]] == ["zealand"]


def test_derived_vocabulary_ignores_caption_only_annotations(tmp_path):
    _write_annotations(tmp_path, [
        {"caption": "Captiononly word."},
        {"caption": "Captiononly label."},
    ])

    artifact = build_description_candidates(str(tmp_path))

    assert artifact["fields"]["description"] == {}


def test_derived_vocabulary_query_is_index_only_and_separate(tmp_path, monkeypatch):
    _write_annotations(tmp_path, [
        {"description": "Two wagons pass."},
        {"description": "A wagon waits."},
    ])
    build_description_candidates(str(tmp_path))
    monkeypatch.setattr(
        "data.metadata.get_metadata",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("raw scan")),
    )

    result = search_mod.vocabulary_from_index(
        "--all", str(tmp_path), family="derived", sort="count"
    )

    assert result == {"status": "ready", "items": [{"value": "wagon", "count": 2, "provenance": {
        "count": 2,
        "document_frequency": 2,
        "aliases": ["wagon", "wagons"],
        "origin": "derived",
        "source_fields": ["description"],
        "normalization": "free_text_terms_v4",
        "quality": 0.5,
    }}]}
