import pytest
import hashlib
import json
import sqlite3

from services.silhouette_identity import canonical_shot, label_slug, normalize_reference, old_object_ordinal, path_length_summary, plan_empty_catalog_parents, proposed_directory, slug_collisions, tentative_instance_id


@pytest.mark.parametrize("label,expected", [(" Brown  Hat ", "brown-hat"), ("brown_hat", "brown-hat"), ("brown---hat", "brown-hat"), ("bird", "bird"), ("A/B\\C:*?", "a-b-c"), ("caf\u00e9", "caf\u00e9"), ("...", "")])
def test_deterministic_shared_slug(label, expected):
    assert label_slug(label) == expected
    assert label_slug(label) == label_slug(label)


def test_slug_collision_no_arbitrary_choice():
    values = ["brown hat", "brown-hat", "Brown Hat", "brown_hat"]
    assert slug_collisions(values) == {"brown-hat": sorted(values)}


def test_shot_and_tentative_instance():
    shot = "tmdb_61044@f078383-f078476"
    assert tentative_instance_id(shot, "object_0002") == shot + "#0002"
    assert canonical_shot("game_rdr2_ce5e0bba@f1012726-f1013511")
    assert old_object_ordinal("object_10000") == 10000


@pytest.mark.parametrize("shot", ["shot", "tmdb_1@f000020-f000010", "tmdb_1@f0000010-f0000020", "../escape@f000001-f000002"])
def test_missing_ambiguous_shot_fails(shot):
    with pytest.raises(ValueError):
        canonical_shot(shot)


def test_media_mismatch_fails():
    with pytest.raises(ValueError):
        canonical_shot("tmdb_1@f000001-f000002", "tmdb_2")


def test_path_construction_and_lengths():
    directory = proposed_directory("movie", "animals", "Bird", "tmdb_1@f000001-f000002#0002")
    assert directory == "data/silhouettes/movie/animals/bird/tmdb_1@f000001-f000002#0002"
    assert "catalog" not in directory
    assert path_length_summary(["a", "abc", "abcd"]) == {"count": 3, "min": 1, "median": 3, "p95": 4, "max": 4}


def test_absolute_relative_foreign_root_and_filename_references(tmp_path):
    relative = "data/silhouettes/catalog/movie/Film/bird/object_0002.png"
    assert normalize_reference(tmp_path, relative) == relative
    assert normalize_reference(tmp_path, "/old/machine/" + relative) == relative
    assert normalize_reference(tmp_path, "object_0002.png", tmp_path / "data/silhouettes/catalog/movie/Film/bird") == relative
    assert normalize_reference(tmp_path, "../escape.png", tmp_path) is None


def test_cleanup_plan_only_empty_ancestors_read_only(tmp_path):
    catalog = tmp_path / "catalog/movie"
    mode = catalog / "Film/bird/object_0002/frame"
    mode.mkdir(parents=True)
    asset = mode / "engraving.png"
    asset.write_bytes(b"image")
    kept = catalog / "OtherFilm/bird/object_0001/frame"
    kept.mkdir(parents=True)
    (kept / "run.json").write_text('{}')
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    plan = plan_empty_catalog_parents(catalog, [asset])
    assert [str(mode), str(mode.parent), str(mode.parent.parent), str(mode.parent.parent.parent)] == plan
    assert str(catalog) not in plan
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    assert asset.read_bytes() == b"image"
    with pytest.raises(ValueError):
        plan_empty_catalog_parents(catalog, [tmp_path / "outside"])


def test_full_fixture_census_collisions_orphans_references_and_safety(tmp_path):
    from services.silhouette_identity_census import run_census

    (tmp_path / "outputs/tests").mkdir(parents=True)
    cases = []
    shot = "tmdb_1@f000001-f000002"
    for label, field, state in (("bird", "animals", "active"), ("horse", "animals", "superseded"), ("brown hat", "wearing", "active"), ("brown-hat", "wearing", "active")):
        directory = tmp_path / "data/silhouettes/catalog/movie/Film" / label.replace(" ", "_")
        directory.mkdir(parents=True)
        metadata_path = directory / "object_0001.json"
        png = directory / "object_0001.png"
        png.write_bytes(b"not decoded as image")
        metadata = {"media_type": "movie", "media_id": "tmdb_1", "shot_id": shot, "frame": 1, "field": field, "label": label, "png": png.name, "assignment": {"state": state}}
        metadata_path.write_text(json.dumps(metadata))
        cases.append({**metadata, "path": metadata_path.relative_to(tmp_path).as_posix()})
    orphan = tmp_path / "data/silhouettes/catalog/movie/Film/bird/object_0002.png"
    orphan.write_bytes(b"orphan")
    bad = orphan.with_suffix('.json')
    bad.write_text('{broken')
    index_path = tmp_path / "data/indexes/illustration/movie-silhouettes.sqlite3"
    index_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(index_path)
    connection.execute("CREATE TABLE records(payload TEXT)")
    for case in cases:
        connection.execute("INSERT INTO records VALUES(?)", (json.dumps(case),))
    connection.execute("INSERT INTO records VALUES(?)", (json.dumps({"path": "data/silhouettes/catalog/movie/Missing/bird/object_0001.json"}),))
    connection.commit()
    connection.close()
    reference = tmp_path / "outputs/tests/benchmark-reference.json"
    reference.write_text(json.dumps({"source": "/foreign/root/" + cases[0]["path"]}))
    import csv
    with (reference.with_suffix(".csv")).open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["record_path", "provenance"])
        writer.writeheader()
        writer.writerow({"record_path": cases[0]["path"], "provenance": json.dumps({"source_json": "/foreign/" + cases[0]["path"]})})
    before = {str(path.relative_to(tmp_path)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in tmp_path.rglob('*') if path.is_file()}
    output = run_census(tmp_path)
    summary = json.loads((output / "summary.json").read_text())
    assert summary["records"] == 4
    assert summary["identity_collision_groups"] == 1
    assert summary["identity_collision_records"] == 4
    assert summary["destination_collision_groups"] == 1
    assert summary["slug_collision_groups"] == 1
    assert summary["malformed_silhouette_metadata"] == 1
    assert summary["pngs_without_records"] == 1
    assert summary["index_rows_without_records"] == 1
    assert summary["assignment"] == {"active": 3, "superseded": 1}
    plan = json.loads((output / "migration-plan.json").read_text())
    assert plan["apply_implemented"] is False
    assert len(plan["records"]) == 4
    assert all(record["status"] == "collision" for record in plan["records"])
    assert all(record["references_found"]["count"] >= 1 for record in plan["records"])
    assert plan["records"][0]["references_found"]["count"] >= 3
    assert json.loads((output / "safety-after.json").read_text())["zero_protected_file_changes"]
    report = (output / "report.md").read_text()
    assert "| identity_collision_records | 4 |" in report
    assert "## 11. Recommendation" in report
    assert "source_json.stem" in report
    for relative, expected in before.items():
        path = tmp_path / relative
        assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == expected
    from services.silhouette_identity_census import resume_verification
    baseline_bytes = (output / "safety-before.json").read_bytes()
    manifest_bytes = (output / "migration-plan.json").read_bytes()
    assert resume_verification(tmp_path) == output
    assert (output / "safety-before.json").read_bytes() == baseline_bytes
    assert (output / "migration-plan.json").read_bytes() == manifest_bytes
    from services.silhouette_identity_census import complete_reference_audit
    assert complete_reference_audit(tmp_path) == output
    assert (output / "migration-plan.json").read_bytes() == manifest_bytes
    orphan.unlink()
    with pytest.raises(RuntimeError, match="Protected files changed"):
        resume_verification(tmp_path)
    assert orphan.relative_to(tmp_path).as_posix() in json.loads((output / "safety-after.json").read_text())["missing_files"]
    with pytest.raises(FileExistsError):
        run_census(tmp_path)


def test_record_missing_and_ambiguous_identity():
    from services.silhouette_identity_census import interpret_record

    path = "data/silhouettes/catalog/movie/Film/bird/object_0002.json"
    metadata = {"media_type": "movie", "media_id": "tmdb_1", "shot_id": "tmdb_1@f000001-f000002", "frame": 1, "field": "animals", "label": "bird", "png": "object_0002.png"}
    assert interpret_record(path, metadata)["status"] == "needs-review"
    assert interpret_record(path, {**metadata, "object_id": "object_0003"})["status"] == "ambiguous"
    assert interpret_record(path, {**metadata, "shot_id": ""})["status"] == "missing-identity"
    assert interpret_record(path, {**metadata, "frame": 3})["status"] == "ambiguous"


@pytest.mark.parametrize("ancestor", ["outputs", "outputs/tests"])
def test_report_symlink_ancestor_rejected_before_writes(tmp_path, ancestor):
    from services.silhouette_identity_census import run_census

    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    linked = project / ancestor
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(external, target_is_directory=True)
    if ancestor == "outputs":
        (external / "tests").mkdir()
    with pytest.raises(ValueError, match="symlinks"):
        run_census(project)
    assert not list(external.rglob("census.sqlite3"))
    assert not list(external.rglob("silhouette-identity-migration"))