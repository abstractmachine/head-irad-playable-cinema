import hashlib
import json
import sqlite3

import pytest

from services.silhouette_archive_reference import ArchiveReferenceError, resolve_silhouette_reference


def _fixture(tmp_path):
    original = "data/silhouettes/catalog/movie/Film/coat/object_0001.json"
    destination = original.replace("data/", "archive/", 1)
    archived = tmp_path / destination
    archived.parent.mkdir(parents=True)
    archived.write_text(json.dumps({"assignment": {"state": "superseded"}}))
    lookup = tmp_path / "lookup.sqlite3"
    connection = sqlite3.connect(lookup)
    connection.execute("CREATE TABLE assets(path TEXT,destination TEXT,sha256 TEXT)")
    connection.execute("INSERT INTO assets VALUES(?,?,?)", (original, destination, hashlib.sha256(archived.read_bytes()).hexdigest()))
    connection.commit()
    connection.close()
    return original, archived, lookup


def test_archive_is_explicit_and_checksum_verified(tmp_path):
    original, archived, lookup = _fixture(tmp_path)
    with pytest.raises(ArchiveReferenceError, match="fallback is disabled"):
        resolve_silhouette_reference(tmp_path, original, lookup=lookup)
    resolved = resolve_silhouette_reference(tmp_path, original, include_archive=True, lookup=lookup)
    assert resolved["path"] == archived
    assert resolved["status"] == "archived"
    assert resolved["checksum_verified"]


def test_original_active_and_historical_access(tmp_path):
    original, archived, lookup = _fixture(tmp_path)
    source = tmp_path / original
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"assignment": {"state": "active"}}))
    assert resolve_silhouette_reference(tmp_path, original)["status"] == "original"
    source.write_text(json.dumps({"assignment": {"state": "superseded"}}))
    with pytest.raises(ArchiveReferenceError) as exc:
        resolve_silhouette_reference(tmp_path, original)
    assert exc.value.code == "NOT_ACTIVE"
    assert resolve_silhouette_reference(tmp_path, original, include_archive=True)["path"] == source


@pytest.mark.parametrize("case,code", [("absent", "MAPPING_MISSING"), ("ambiguous", "MAPPING_AMBIGUOUS"), ("missing", "ARCHIVE_MISSING"), ("changed", "CHECKSUM_MISMATCH")])
def test_mapping_failures_are_explicit(tmp_path, case, code):
    original, archived, lookup = _fixture(tmp_path)
    if case in ("absent", "ambiguous"):
        connection = sqlite3.connect(lookup)
        connection.execute("DELETE FROM assets" if case == "absent" else "INSERT INTO assets SELECT * FROM assets")
        connection.commit()
        connection.close()
    elif case == "missing":
        archived.unlink()
    else:
        archived.write_text("changed")
    with pytest.raises(ArchiveReferenceError) as exc:
        resolve_silhouette_reference(tmp_path, original, include_archive=True, lookup=lookup)
    assert exc.value.code == code


def test_reserved_ordinals_survive_restart_and_failed_image_write(tmp_path):
    from services.silhouette_catalog import _next_object_index
    from services.silhouette_legacy_allocation import initialize_namespace

    namespace = tmp_path / "data/silhouettes/catalog/movie/Film/coat"
    namespace.mkdir(parents=True)
    (namespace / "object_0002.png").write_bytes(b"active")
    initialize_namespace(namespace, 9)
    assert _next_object_index(namespace, count=3) == 10
    assert _next_object_index(namespace) == 13
    initialize_namespace(namespace, 4)
    assert _next_object_index(namespace) == 14
    assert (namespace / "object_0002.png").read_bytes() == b"active"


def test_archive_without_registry_fails_closed(tmp_path):
    from services.silhouette_catalog import _next_object_index

    namespace = tmp_path / "data/silhouettes/catalog/movie/Film/coat"
    namespace.mkdir(parents=True)
    (tmp_path / "archive/silhouettes").mkdir(parents=True)
    with pytest.raises(ValueError, match="initialization required"):
        _next_object_index(namespace)


def test_concurrent_reservations_are_disjoint(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from services.silhouette_legacy_allocation import initialize_namespace, reserve_legacy_ordinals

    initialize_namespace(tmp_path, 100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        starts = list(pool.map(lambda number: reserve_legacy_ordinals(tmp_path, count=2), range(30)))
    assert sorted(starts) == list(range(101, 161, 2))


def test_failed_registry_commit_preserves_high_water(tmp_path, monkeypatch):
    import data.annotate
    from services.silhouette_legacy_allocation import MARKER, initialize_namespace, reserve_legacy_ordinals

    initialize_namespace(tmp_path, 10)
    before = (tmp_path / MARKER).read_bytes()
    def fail(*args):
        raise OSError("write failed")
    monkeypatch.setattr(data.annotate, "atomic_write_text", fail)
    with pytest.raises(OSError):
        reserve_legacy_ordinals(tmp_path)
    assert (tmp_path / MARKER).read_bytes() == before


def test_historical_csv_and_jsonl_recover_without_rewriting(tmp_path):
    import csv
    from services.silhouette_archive_reference import resolve_historical_report_reference

    original, archived, lookup = _fixture(tmp_path)
    report = tmp_path / "old-report.csv"
    report.write_text("historical_json_path\n" + str(tmp_path / original) + "\n")
    before = (report.read_bytes(), report.stat().st_mtime_ns)
    with report.open() as handle:
        row = next(csv.DictReader(handle))
    assert resolve_historical_report_reference(tmp_path, row, field="historical_json_path", lookup=lookup)["path"] == archived
    assert (report.read_bytes(), report.stat().st_mtime_ns) == before
    report = tmp_path / "old-report.jsonl"
    report.write_text(json.dumps({"record_path": original}) + "\n")
    before = (report.read_bytes(), report.stat().st_mtime_ns)
    row = json.loads(report.read_text())
    assert resolve_historical_report_reference(tmp_path, row, field="record_path", lookup=lookup)["status"] == "archived"
    assert (report.read_bytes(), report.stat().st_mtime_ns) == before


def test_multiple_predecessors_retirement_and_chains(tmp_path):
    from services.silhouette_archive_reference import inspect_historical_lineage

    original, archived, lookup = _fixture(tmp_path)
    active = tmp_path / original.replace("0001", "0003")
    active.parent.mkdir(parents=True)
    first = {"media_type": "movie", "filename_stem": "Film", "label": "coat", "object_id": "object_0001"}
    missing = dict(first, object_id="object_0002")
    active.write_text(json.dumps({"assignment": {"state": "active", "supersedes": [first, missing]}}))
    packet = inspect_historical_lineage(tmp_path, active, lookup=lookup)
    assert [item["status"] for item in packet["predecessors"]] == ["archived", "error"]
    assert packet["predecessors"][1]["code"] == "MAPPING_MISSING"
    assert inspect_historical_lineage(tmp_path, original, lookup=lookup)["replacements"] == []
    archived.write_text(json.dumps({"assignment": {"state": "superseded", "superseded_by": dict(first, object_id="object_0003")}}))
    connection = sqlite3.connect(lookup)
    connection.execute("UPDATE assets SET sha256=?", (hashlib.sha256(archived.read_bytes()).hexdigest(),))
    connection.commit()
    connection.close()
    assert inspect_historical_lineage(tmp_path, original, lookup=lookup)["replacements"][0]["path"] == active


def test_reference_cli_delegates_and_requires_explicit_history(tmp_path, monkeypatch, capsys):
    import cli
    import services.silhouette_archive_reference as resolver

    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args: pytest.fail("Preferences must not change"))
    monkeypatch.setattr(resolver, "resolve_silhouette_reference", lambda project, reference, **kwargs: {"service_result": kwargs["include_archive"]})
    args = cli.build_parser().parse_args(["silhouette", "inspect-reference", "record.json", "--project", str(tmp_path), "--include-archive"])
    args.func(args)
    assert json.loads(capsys.readouterr().out) == {"service_result": True}
    args.lineage = True
    args.include_archive = False
    with pytest.raises(SystemExit, match="requires explicit"):
        args.func(args)


@pytest.mark.parametrize("reference", ["data/silhouettes/catalog/movie/Film/coat/../object_0001.json", "data/silhouettes/catalog/movie/Film/coat/object_0001.mask.png", "data/silhouettes/catalog/other/Film/coat/object_0001.json"])
def test_noncanonical_reference_paths_are_rejected(tmp_path, reference):
    with pytest.raises(ArchiveReferenceError) as exc:
        resolve_silhouette_reference(tmp_path, reference, include_archive=True)
    assert exc.value.code == "INVALID_PATH"


def test_symlink_archive_target_is_never_followed(tmp_path):
    original, archived, lookup = _fixture(tmp_path)
    foreign = tmp_path / "foreign.json"
    foreign.write_bytes(archived.read_bytes())
    archived.unlink()
    archived.symlink_to(foreign)
    with pytest.raises(ArchiveReferenceError) as exc:
        resolve_silhouette_reference(tmp_path, original, include_archive=True, lookup=lookup)
    assert exc.value.code == "UNSAFE_PATH"


def test_archived_png_requires_matching_metadata_mapping(tmp_path):
    original, archived, lookup = _fixture(tmp_path)
    png_original = original.replace(".json", ".png")
    png = archived.with_suffix(".png")
    png.write_bytes(b"historical pixels")
    connection = sqlite3.connect(lookup)
    connection.execute("INSERT INTO assets VALUES(?,?,?)", (png_original, png.relative_to(tmp_path).as_posix(), hashlib.sha256(png.read_bytes()).hexdigest()))
    connection.commit()
    connection.close()
    assert resolve_silhouette_reference(tmp_path, png_original, include_archive=True, lookup=lookup)["path"] == png
    archived.unlink()
    with pytest.raises(ArchiveReferenceError):
        resolve_silhouette_reference(tmp_path, png_original, include_archive=True, lookup=lookup)


def test_engraving_best_selection_excludes_retired_records(tmp_path):
    from services.engraving_batch import scan_best_silhouettes

    directory = tmp_path / "data/silhouettes/catalog/movie/Film/coat"
    directory.mkdir(parents=True)
    for number, state in enumerate(("active", "inactive", "superseded"), 1):
        (directory / f"object_{number:04d}.json").write_text(json.dumps({"human_best": True, "assignment": {"state": state}, "label": "coat"}))
    before = {path: path.read_bytes() for path in directory.iterdir()}
    assert [item["path"].stem for item in scan_best_silhouettes(str(tmp_path))] == ["object_0001"]
    assert {path: path.read_bytes() for path in directory.iterdir()} == before


@pytest.mark.parametrize("state", ["inactive", "superseded"])
def test_direct_consumers_reject_history_before_work(tmp_path, state):
    from services.engraving_generate_openai import generate_engraving_openai
    from services.engraving_smoke import prepare_engraving_from_source
    from services.engraving_local_pipeline import convert_one
    from services.annotation_qa_qwen_vl import prepare_cases
    from services.engraving_benchmark_evidence import shot_evidence_for_record

    source = tmp_path / "object_0001.json"
    record = {"assignment": {"state": state}}
    source.write_text(json.dumps(record))
    before = source.read_bytes()
    for consumer in (generate_engraving_openai, prepare_engraving_from_source, convert_one):
        with pytest.raises(ValueError, match="active record"):
            consumer(tmp_path, source)
    with pytest.raises(ValueError, match="active record"):
        prepare_cases(tmp_path, tmp_path / "qa", {"candidates": [{"source": {"record_path": source.name}}]}, {})
    with pytest.raises(ValueError, match="active record"):
        shot_evidence_for_record(tmp_path, record)
    assert source.read_bytes() == before
    assert not (tmp_path / "qa").exists()


def test_direct_loader_cannot_consume_archive_even_if_marked_active(tmp_path):
    from services.silhouette_catalog import load_active_record

    source = tmp_path / "archive/silhouettes/object_0001.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"assignment": {"state": "active"}}))
    with pytest.raises(ValueError, match="cannot read archive"):
        load_active_record(source, tmp_path)


def test_catalog_scans_are_active_only_and_history_is_explicit(tmp_path):
    from services.silhouette_catalog import iter_catalog, scan_catalog, audit_catalog

    directory = tmp_path / "data/silhouettes/catalog/movie/Film/coat"
    directory.mkdir(parents=True)
    for number, state in enumerate(("active", "inactive", "superseded"), 1):
        (directory / f"object_{number:04d}.json").write_text(json.dumps({"assignment": {"state": state}, "label": "coat"}))
    archived = tmp_path / "archive/silhouettes/catalog/movie/Film/coat"
    archived.mkdir(parents=True)
    (archived / "object_0099.json").write_text("{}")
    assert len(scan_catalog(str(tmp_path))) == 1
    assert len(list(iter_catalog(str(tmp_path), label="coat"))) == 1
    assert len(scan_catalog(str(tmp_path), include_history=True)) == 3
    assert audit_catalog(str(tmp_path))["total_objects"] == 3


def test_central_registry_preparation_publication_and_concurrency(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from test_silhouette_archive import _project, _unchanged
    from services.silhouette_legacy_allocation import prepare_registry, publish_registry, registry_path, registry_status
    from services.silhouette_catalog import _next_object_index

    project = _project(tmp_path)
    (project / "data/silhouettes/.catalog-extraction.lock").touch()
    census = project / "outputs/tests/silhouette-identity-migration/census.sqlite3"
    prepared = tmp_path / "prepared.sqlite3"
    before = _unchanged(project)
    verification = prepare_registry(project, census, prepared)
    assert verification["records"] == 3
    assert verification["allocated_new_ids"] == 0
    assert not registry_path(project).exists()
    publish_registry(project, census, prepared)
    assert registry_status(project, census)["ready"]
    after = _unchanged(project)
    assert all(after[key] == value for key, value in before.items())
    namespace = project / "data/silhouettes/catalog/movie/Film/coat"
    with ThreadPoolExecutor(max_workers=4) as pool:
        starts = list(pool.map(lambda number: _next_object_index(namespace, count=2), range(10)))
    assert sorted(starts) == list(range(4, 24, 2))
    assert _next_object_index(namespace) == 24
    assert registry_status(project, census)["unreserved_existing_ids"] == 0
    with pytest.raises(FileExistsError):
        publish_registry(project, census, prepared)


def test_book_direct_insertion_rejects_retired_asset_before_copy(tmp_path):
    from types import SimpleNamespace
    from visualizers.book_visualizer import BookVisualizerWindow

    source = tmp_path / "object_0001.json"
    source.write_text(json.dumps({"assignment": {"state": "superseded"}}))
    window = SimpleNamespace(_doc=object(), _slug="test", _project_path=str(tmp_path))
    with pytest.raises(ValueError, match="active record"):
        BookVisualizerWindow._insert_silhouette_at(window, str(source.with_suffix(".png")), {}, 0, 0.5, 0.5)
    assert not (tmp_path / "outputs/books").exists()


def test_book_generated_engraving_insertion_preserves_existing_workflow(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import visualizers.book_visualizer as book
    from data.book import book_dir

    source = tmp_path / "data/engravings/catalog/movie/Film/bird/object_0001/frame/bird.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"existing generated pixels")
    overlay = SimpleNamespace(_layers=[], current_layers=lambda: [], update=lambda: None)
    committed = []
    window = SimpleNamespace(_doc=object(), _slug="test", _project_path=str(tmp_path), _overlay=overlay, _spread_idx=0, _on_layer_committed=committed.append)
    monkeypatch.setattr(book, "QPixmap", lambda *args: SimpleNamespace(isNull=lambda: True))
    book.BookVisualizerWindow._insert_silhouette_at(window, str(source), {"label": "bird"}, 0, 0.5, 0.5)
    assert len(committed) == 1
    assert (book_dir(str(tmp_path), "test") / committed[0]["source"]).read_bytes() == source.read_bytes()


def test_registry_publication_failure_is_reversible_and_corpus_intact(tmp_path, monkeypatch):
    from test_silhouette_archive import _project, _unchanged
    import services.silhouette_legacy_allocation as allocation

    project = _project(tmp_path)
    (project / "data/silhouettes/.catalog-extraction.lock").touch()
    census = project / "outputs/tests/silhouette-identity-migration/census.sqlite3"
    prepared = tmp_path / "prepared.sqlite3"
    allocation.prepare_registry(project, census, prepared)
    before = _unchanged(project)
    real_replace = allocation.os.replace
    def fail(source, destination):
        raise OSError("publication failed")
    monkeypatch.setattr(allocation.os, "replace", fail)
    with pytest.raises(OSError):
        allocation.publish_registry(project, census, prepared)
    assert not allocation.registry_path(project).exists()
    assert _unchanged(project) == before
    monkeypatch.setattr(allocation.os, "replace", real_replace)
    allocation.publish_registry(project, census, prepared)
    real_replace(allocation.registry_path(project), tmp_path / "rollback.sqlite3")
    assert _unchanged(project) == before


def test_registry_refuses_uncensused_namespace(tmp_path):
    from test_silhouette_archive import _project
    from services.silhouette_legacy_allocation import prepare_registry

    project = _project(tmp_path)
    new = project / "data/silhouettes/catalog/movie/Other/hat"
    new.mkdir(parents=True)
    (new / "object_0001.json").write_text("{}")
    with pytest.raises(ValueError, match="Uncensused"):
        prepare_registry(project, project / "outputs/tests/silhouette-identity-migration/census.sqlite3", tmp_path / "prepared.sqlite3")


def test_legacy_png_generator_and_evidence_cannot_bypass_record_state(tmp_path):
    from services.engraving_generate import generate_openai_engraving
    from services.engraving_benchmark_evidence import shot_evidence_for_record

    source = tmp_path / "data/silhouettes/catalog/movie/Film/coat/object_0001.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"assignment": {"state": "superseded"}}))
    with pytest.raises(ValueError, match="active record"):
        generate_openai_engraving(project_path=str(tmp_path), silhouette_png_path=str(source.with_suffix(".png")), engraving_id="test", cache_dir=tmp_path)
    with pytest.raises(ValueError, match="active record"):
        shot_evidence_for_record(tmp_path, {"record_path": source.relative_to(tmp_path).as_posix()})


def test_active_qa_preparation_after_simulated_archive(tmp_path, monkeypatch):
    from PIL import Image
    from services.annotation_qa_qwen_vl import prepare_cases

    source = tmp_path / "data/silhouettes/catalog/movie/Film/coat/object_0001.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"assignment": {"state": "active", "supersedes": {"object_id": "object_0002"}}, "frame": 5, "png": "object_0001.png"}))
    Image.new("RGBA", (8, 8), (0, 0, 0, 255)).save(source.with_suffix(".png"))
    archived = tmp_path / "archive/silhouettes/catalog/movie/Film/coat/object_0002.json"
    archived.parent.mkdir(parents=True)
    archived.write_text(json.dumps({"assignment": {"state": "superseded"}}))
    annotations = tmp_path / "annotations.json"
    shot_id = "tmdb_1@f000001-f000010"
    annotations.write_text(json.dumps([{"shot": {"shot_id": shot_id, "annotation": {"wearing": ["coat", "hat", "holster", "belt", "boots"]}, "best_frame": {"frame": 5}}}]))
    cached = tmp_path / "frame.png"
    Image.new("RGB", (8, 8)).save(cached)
    monkeypatch.setattr("data.annotate.get_annotation_json_path", lambda *args: annotations)
    monkeypatch.setattr("services.frame_match.best_frame_path", lambda *args: cached)
    candidate = {"benchmark_id": "active", "evidence": {}, "source": {"filename": "Film.mp4", "media_type": "movie", "shot_id": shot_id, "field": "wearing", "label": "coat", "frame": 5, "record_path": source.relative_to(tmp_path).as_posix()}}
    before = source.read_bytes()
    cases, _ = prepare_cases(tmp_path, tmp_path / "qa", {"candidates": [candidate]}, {})
    assert len(cases) == 1 and cases[0]["assignment_state"] == "active"
    assert source.read_bytes() == before
    candidate["source"]["record_path"] = archived.relative_to(tmp_path).as_posix()
    with pytest.raises(ValueError, match="active record"):
        prepare_cases(tmp_path, tmp_path / "qa2", {"candidates": [candidate]}, {})


def test_book_local_copy_stays_usable_after_source_archival(tmp_path):
    from services.silhouette_archive_reference import resolve_silhouette_reference

    original, archived, lookup = _fixture(tmp_path)
    local = tmp_path / "outputs/books/test/illustrations/silhouettes/coat.png"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"copied book pixels")
    layers = tmp_path / "outputs/books/test/layers.json"
    layers.write_text(json.dumps({"source": "illustrations/silhouettes/coat.png"}))
    before = (local.read_bytes(), layers.read_bytes())
    assert resolve_silhouette_reference(tmp_path, original, include_archive=True, lookup=lookup)["status"] == "archived"
    assert (layers.parent / json.loads(layers.read_text())["source"]).read_bytes() == before[0]
    assert (local.read_bytes(), layers.read_bytes()) == before


def test_preparation_report_requires_verified_registry(tmp_path):
    from test_silhouette_archive import _project
    from services.silhouette_archive import plan_archive
    from services.silhouette_legacy_allocation import prepare_registry, publish_registry

    project = _project(tmp_path)
    lock = project / "data/silhouettes/.catalog-extraction.lock"
    lock.touch()
    from services.silhouette_identity_census import fingerprint
    info, _ = fingerprint(lock)
    census = project / "outputs/tests/silhouette-identity-migration/census.sqlite3"
    with sqlite3.connect(census) as database:
        database.execute("INSERT INTO files(path,domain,size,mtime_ns,sha256,kind) VALUES(?,?,?,?,?,?)", (lock.relative_to(project).as_posix(), "data/silhouettes", info["size"], info["mtime_ns"], info["sha256"], info["kind"]))
    plan_archive(project)
    output = plan_archive(project, version="v3-before", revalidate=True, preparation=True)
    assert json.loads((output / "summary.json").read_text())["status"].startswith("BLOCKED")
    census = project / "outputs/tests/silhouette-identity-migration/census.sqlite3"
    prepared = tmp_path / "registry.sqlite3"
    prepare_registry(project, census, prepared)
    publish_registry(project, census, prepared)
    output = plan_archive(project, version="v3-after", revalidate=True, preparation=True)
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "READY FOR ARCHIVAL REVIEW"
    assert not summary["namespace_integrity"]["domain_count_differences"]
    assert not summary["stage1_preparation"]["index_transition"]["production_rebuild_performed"]