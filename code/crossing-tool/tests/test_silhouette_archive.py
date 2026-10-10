import pytest
import hashlib
import json
import sqlite3

from services.silhouette_archive import archive_path, eligibility, selected


@pytest.mark.parametrize("state,expected", [("active", False), ("inactive", False), ("superseded", True), ("unknown", False)])
def test_superseded_only(state, expected):
    assert selected({"assignment": {"state": state}}) is expected
    assert not selected({})


def test_preserve_relative_hierarchy():
    assert archive_path("data/silhouettes/catalog/movie/Film/brown_coat/object_0002.png") == "archive/silhouettes/catalog/movie/Film/brown_coat/object_0002.png"
    for bad in ("/data/silhouettes/a.png", "data/silhouettes/../a.png", "data/engravings/a.png"):
        with pytest.raises(ValueError):
            archive_path(bad)


def test_dependency_classes():
    assert eligibility([]) == "A_safe_to_archive"
    assert eligibility([], 2) == "E_historical_dependency"
    assert eligibility(["missing_png"]) == "D_needs_review"
    assert eligibility(["active_dependency:engraving"]) == "B_active_dependency_blocking"
    assert eligibility(["shared_with_active", "active_dependency:engraving"]) == "C_shared_active_blocking"


def _project(tmp_path, *, missing=None, shared=False, referenced=False, collision=False, count=1):
    from services.silhouette_identity_census import create_tables, fingerprint, interpret_record

    project = tmp_path / "project"
    evidence = project / "outputs/tests/silhouette-identity-migration"
    evidence.mkdir(parents=True)
    catalog = project / "data/silhouettes/catalog/movie/Film/coat"
    catalog.mkdir(parents=True)
    database = sqlite3.connect(evidence / "census.sqlite3")
    create_tables(database)
    for number, state in enumerate(["superseded"] * count + ["active", "inactive"], 1):
        png = catalog / f"object_{number:04d}.png"
        metadata_path = png.with_suffix(".json")
        png.write_bytes(b"mask")
        metadata = {"media_type": "movie", "filename_stem": "Film", "media_id": "tmdb_1", "shot_id": "tmdb_1@f000001-f000002", "frame": 1, "field": "wearing", "label": "coat", "png": "object_0001.png" if shared and state == "active" else png.name, "assignment": {"state": state}}
        metadata_path.write_text(json.dumps(metadata))
        relative = metadata_path.relative_to(project).as_posix()
        record = interpret_record(relative, metadata)
        database.execute("INSERT INTO records VALUES(" + ",".join("?" for _ in record) + ")", tuple(record.values()))
    for path in catalog.iterdir():
        info, _ = fingerprint(path)
        relative = path.relative_to(project).as_posix()
        database.execute("INSERT INTO files(path,domain,size,mtime_ns,sha256,kind) VALUES(?,?,?,?,?,?)", (relative, "data/silhouettes", info["size"], info["mtime_ns"], info["sha256"], info["kind"]))
    if referenced:
        database.execute("INSERT INTO refs(source,pointer,value,normalized,kind,subsystem) VALUES(?,?,?,?,?,?)", ("data/annotations/shots/movie/Film.json", "/mask", "object_0001.png", "data/silhouettes/catalog/movie/Film/coat/object_0001.png", "relative", "data/annotations"))
    database.execute("CREATE INDEX record_png ON records(png_path)")
    database.execute("CREATE INDEX ref_target ON refs(normalized)")
    database.commit()
    database.close()
    (evidence / "safety-after.json").write_text(json.dumps({"zero_protected_file_changes": True}))
    if missing:
        (catalog / ("object_0001." + missing)).unlink()
    if collision:
        target = project / "archive/silhouettes/catalog/movie/Film/coat/object_0001.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing archive evidence")
    return project


def _unchanged(project):
    return {path.relative_to(project).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for root in ("data", "archive", "outputs/tests/silhouette-identity-migration") for path in (project / root).rglob("*") if path.is_file()}


@pytest.mark.parametrize("option,reason", [("missing_metadata", "metadata_unverified:FileNotFoundError"), ("missing_png", "missing_or_unsafe_source"), ("shared", "shared_with_active"), ("referenced", "active_dependency:annotation"), ("collision", "destination_collision")])
def test_edge_cases_read_only(tmp_path, option, reason):
    from services.silhouette_archive import plan_archive

    args = {"missing": "json" if option == "missing_metadata" else "png"} if option.startswith("missing_") else {option: True}
    project = _project(tmp_path, **args)
    before = _unchanged(project)
    output = plan_archive(project)
    records = [json.loads(line) for line in (output / "archive-plan.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["assignment_state"] == "superseded"
    assert reason in records[0]["blocking_reasons"]
    assert _unchanged(project) == before
    assert json.loads((output / "summary.json").read_text())["status"].startswith("BLOCKED")


def test_streaming_complete_selection_no_archive_or_indexes_mutated(tmp_path):
    from services.silhouette_archive import plan_archive

    project = _project(tmp_path, count=2000)
    before = _unchanged(project)
    output = plan_archive(project)
    with (output / "archive-plan.jsonl").open() as handle:
        paths = [json.loads(line)["source_metadata_path"] for line in handle]
    assert len(paths) == 2000
    assert paths == sorted(paths)
    assert _unchanged(project) == before
    assert not (project / "archive").exists()
    summary = json.loads((output / "summary.json").read_text())
    assert summary["census_population"] == {"active": 1, "inactive": 1, "superseded": 2000}
    assert summary["unique_files"] == 4000
    assert summary["eligibility"] == {"A_safe_to_archive": 2000}
    with pytest.raises(FileExistsError):
        plan_archive(project)


def test_manifest_determinism(tmp_path):
    from services.silhouette_archive import plan_archive

    first = _project(tmp_path / "first", count=10)
    second = _project(tmp_path / "second", count=10)
    assert (plan_archive(first) / "archive-plan.jsonl").read_bytes() == (plan_archive(second) / "archive-plan.jsonl").read_bytes()


def test_cli_dry_run_only_uses_service(monkeypatch, tmp_path):
    import cli
    import services.silhouette_archive as service

    monkeypatch.setattr(cli.prefs, "get", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: pytest.fail("Preferences must not be written"))
    calls = []
    monkeypatch.setattr(service, "plan_archive", lambda project: calls.append(project) or tmp_path)
    parser = cli.build_parser()
    args = parser.parse_args(["silhouette", "archive", "--project", str(tmp_path)])
    args.func(args)
    assert calls == [str(tmp_path)]
    with pytest.raises(SystemExit):
        parser.parse_args(["silhouette", "archive", "--apply"])


def test_resolvable_lineage_and_historical_index_self_reference_do_not_block(tmp_path):
    from services.silhouette_archive import plan_archive

    project = _project(tmp_path)
    census = project / "outputs/tests/silhouette-identity-migration/census.sqlite3"
    database = sqlite3.connect(census)
    old = "data/silhouettes/catalog/movie/Film/coat/object_0001.json"
    active = "data/silhouettes/catalog/movie/Film/coat/object_0002.json"
    index = "data/indexes/illustration/movie-silhouettes.sqlite3"
    database.execute("INSERT INTO indexed VALUES(?,?,?,?,?)", (old, index, "1", "superseded", "object_0001"))
    database.execute("INSERT INTO refs(source,pointer,value,normalized,kind,subsystem) VALUES(?,?,?,?,?,?)", (index + "#records[1]", "/path", old, old, "relative", "indexes"))
    database.execute("INSERT INTO refs(source,pointer,value,normalized,kind,subsystem) VALUES(?,?,?,?,?,?)", (active, "/assignment/supersedes", old, old, "identity_reconstructible", "data/silhouettes"))
    database.commit()
    database.close()
    output = plan_archive(project)
    record = json.loads((output / "archive-plan.jsonl").read_text())
    assert record["blocking_reasons"] == []
    assert record["archive_eligibility"] == "E_historical_dependency"
    assert record["dependencies"] == [{"category": "historical_lineage_resolvable", "occurrences": 1, "example_id": 2}]


@pytest.mark.parametrize("pointer,kind", [("/assignment/supersedes", "relative"), ("/mask", "identity_reconstructible")])
def test_runtime_paths_are_not_downgraded(tmp_path, pointer, kind):
    from services.silhouette_archive import plan_archive

    project = _project(tmp_path)
    census = project / "outputs/tests/silhouette-identity-migration/census.sqlite3"
    connection = sqlite3.connect(census)
    original = "data/silhouettes/catalog/movie/Film/coat/object_0001.json"
    active = original.replace("0001", "0002")
    connection.execute("INSERT INTO refs(source,pointer,value,normalized,kind,subsystem) VALUES(?,?,?,?,?,?)", (active, pointer, original, original, kind, "data/silhouettes"))
    connection.commit()
    connection.close()
    record = json.loads((plan_archive(project) / "archive-plan.jsonl").read_text())
    assert record["blocking_reasons"] == ["active_dependency:active_silhouette"]


def test_versioned_revalidation_preserves_evidence_and_requires_deployment(tmp_path):
    from services.silhouette_archive import plan_archive
    from services.silhouette_archive_reference import resolve_silhouette_reference

    project = _project(tmp_path)
    original = plan_archive(project)
    retained = {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in original.iterdir() if path.is_file()}
    protected = _unchanged(project)
    output = plan_archive(project, version="v2", revalidate=True)
    assert output == original / "v2"
    assert {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in original.iterdir() if path.is_file()} == retained
    assert _unchanged(project) == protected
    summary = json.loads((output / "summary.json").read_text())
    assert summary["evidence_reuse"]["candidate_json_rereads"] == 0
    assert summary["task_safety_comparison"]["zero_detected_task_changes"]
    assert not summary["legacy_allocator_compatibility"]["production_deployment_verified"]
    assert summary["status"].startswith("BLOCKED")
    assert resolve_silhouette_reference(project, "data/silhouettes/catalog/movie/Film/coat/object_0001.json", include_archive=True, lookup=output / "plan.sqlite3")["status"] == "original"
    with pytest.raises(FileExistsError):
        plan_archive(project, version="v2", revalidate=True)
    assert not (project / "archive").exists()


def test_revalidation_refuses_changed_catalog_and_unsafe_version(tmp_path):
    from services.silhouette_archive import plan_archive

    project = _project(tmp_path)
    plan_archive(project)
    with pytest.raises(ValueError, match="Version"):
        plan_archive(project, version="../escape", revalidate=True)
    source = project / "data/silhouettes/catalog/movie/Film/coat/object_0001.json"
    source.write_text(source.read_text() + " ")
    before = _unchanged(project)
    with pytest.raises(ValueError, match="drifted"):
        plan_archive(project, version="v2", revalidate=True)
    assert _unchanged(project) == before


def test_cli_versioned_rerun_delegates(tmp_path, monkeypatch):
    import cli
    import services.silhouette_archive as archive

    calls = []
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args: pytest.fail("Preferences must not change"))
    monkeypatch.setattr(archive, "plan_archive", lambda project, **kwargs: calls.append((project, kwargs)) or tmp_path)
    args = cli.build_parser().parse_args(["silhouette", "archive", "--project", str(tmp_path), "--version", "v2", "--revalidate"])
    args.func(args)
    assert calls == [(str(tmp_path), {"version": "v2", "revalidate": True})]


def test_safety_distinguishes_old_baseline_from_task_mutations(tmp_path):
    from services.silhouette_archive import compare_snapshots

    original = {"path": "outputs/books/book/layers.json", "observed": {"size": 4, "mtime_ns": 123, "kind": "regular"}, "sha256": "same_bytes", "issues": ["differs_from_census_metadata"]}
    for phase in ("before", "after"):
        (tmp_path / f"safety-{phase}-files.jsonl").write_text(json.dumps(original) + "\n")
    assert compare_snapshots(tmp_path)["zero_detected_task_changes"]
    changed = {**original, "observed": {**original["observed"], "mtime_ns": 456}}
    (tmp_path / "safety-after-files.jsonl").write_text(json.dumps(changed) + "\n")
    assert not compare_snapshots(tmp_path)["zero_detected_task_changes"]


def test_hardlinked_assets_require_review(tmp_path):
    import os
    from services.silhouette_archive import plan_archive

    project = _project(tmp_path)
    catalog = project / "data/silhouettes/catalog/movie/Film/coat"
    active = catalog / "object_0002.png"
    active.unlink()
    os.link(catalog / "object_0001.png", active)
    before = _unchanged(project)
    output = plan_archive(project)
    record = json.loads((output / "archive-plan.jsonl").read_text())
    assert "hardlinked_source_needs_review" in record["blocking_reasons"]
    assert record["archive_eligibility"] == "D_needs_review"
    assert _unchanged(project) == before


def test_owned_sidecar_preserves_name_without_owning_source_frame(tmp_path):
    from services.silhouette_archive import plan_archive
    from services.silhouette_identity_census import fingerprint

    project = _project(tmp_path)
    sidecar = project / "data/silhouettes/catalog/movie/Film/coat/object_0001.mask.png"
    sidecar.write_bytes(b"sidecar")
    info, _ = fingerprint(sidecar)
    census = project / "outputs/tests/silhouette-identity-migration/census.sqlite3"
    database = sqlite3.connect(census)
    relative = sidecar.relative_to(project).as_posix()
    database.execute("INSERT INTO files(path,domain,size,mtime_ns,sha256,kind) VALUES(?,?,?,?,?,?)", (relative, "data/silhouettes", info["size"], info["mtime_ns"], info["sha256"], info["kind"]))
    database.commit()
    database.close()
    record = json.loads((plan_archive(project) / "archive-plan.jsonl").read_text())
    assert len(record["files"]) == 3
    assert any(item["archive_path"].endswith("/object_0001.mask.png") for item in record["files"])


def test_preexisting_report_symlink_cannot_redirect_writes(tmp_path):
    from services.silhouette_archive import plan_archive

    project = _project(tmp_path)
    output = project / "outputs/tests/silhouette-archive"
    output.mkdir()
    target = project / "data/silhouettes/catalog/movie/Film/coat/object_0002.json"
    (output / "archive-plan.jsonl").symlink_to(target)
    before = _unchanged(project)
    with pytest.raises(FileExistsError):
        plan_archive(project)
    assert _unchanged(project) == before


def test_archived_maximum_requires_ordinal_reservation(tmp_path):
    from services.silhouette_archive import allocator_reservations

    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ':memory:' AS census")
    connection.execute("CREATE TABLE census.records(metadata_path TEXT,object_id TEXT,assignment TEXT)")
    connection.executemany("INSERT INTO census.records VALUES(?,?,?)", [("data/silhouettes/catalog/movie/Film/coat/object_0001.json", "object_0001", "active"), ("data/silhouettes/catalog/movie/Film/coat/object_0002.json", "object_0002", "superseded")])
    result = allocator_reservations(connection, tmp_path)
    assert result["namespaces_at_risk"] == 1
    record = json.loads((tmp_path / "legacy-ordinal-reservations.jsonl").read_text())
    assert record["next_ordinal_must_exceed"] == 2
    assert record["maximum_remaining_ordinal"] == 1