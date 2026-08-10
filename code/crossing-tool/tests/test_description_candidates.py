import json

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

    assert candidate_path(str(tmp_path)).name == "description_candidates_movie.json"
    assert artifact["meta"]["origin"] == "description"
    assert artifact["candidates"]["horse"] == {
        "document_frequency": 2,
        "aliases": ["horse", "horses"],
        "origin": "description",
        "source_field": "description",
        "normalization": "description_terms_v1",
        "quality": 0.5,
    }
    assert artifact["candidates"]["wagon"]["document_frequency"] == 2
    assert "lantern" not in artifact["candidates"]
    assert "fields" not in artifact
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
