import concurrent.futures
import sqlite3
import multiprocessing

import pytest

from data.media_id import build_shot_id
from services.silhouette_canonical_identity import (
    InstanceRegistry, RESEARCH, instance_identifier, label_slug, prepare_registration,
    proposed_artifact_directory,
)


def binding(project, number=1, *, media="movie", label="bird", field="animals", shot=10):
    media_id = "tmdb_61044" if media == "movie" else "game_rdr2_ce5e0bba"
    return prepare_registration({
        "media_type": media, "media_id": media_id, "shot_id": build_shot_id(media_id, shot, shot+10),
        "field": field, "label": label, "filename_stem": "film", "frame": shot+5,
        "path": project / f"data/silhouettes/catalog/{media}/film/{label}/object_{number:04d}.json",
    }, project)


@pytest.fixture
def registry(tmp_path):
    registry = InstanceRegistry(tmp_path, tmp_path / RESEARCH / "registry.sqlite3")
    yield registry
    registry.close()


@pytest.mark.parametrize("media", ["movie", "gameplay"])
def test_canonical_media_and_shot_reuse(tmp_path, registry, media):
    source = binding(tmp_path, media=media)
    instance = registry.register([source])[0][1]
    assert instance == source["shot_id"] + "#0001"


def test_deterministic_total_initial_order(tmp_path, registry):
    records = [binding(tmp_path, number, label=label, field=field)
               for number, label, field in ((2, "bird", "animals"), (1, "branch", "objects"),
                                           (1, "bird", "animals"), (1, "flying", "action"),
                                           (1, "hat", "wearing"), (1, "forest", "setting"))]
    first = dict((key, instance) for key, instance, _ in registry.register(reversed(records)))
    other = InstanceRegistry(tmp_path, tmp_path / RESEARCH / "other.sqlite3")
    try:
        assert first == dict((key, instance) for key, instance, _ in other.register(records))
        assert len(set(first.values())) == 6
    finally:
        other.close()


def test_incremental_earlier_input_and_multiple_shots(tmp_path, registry):
    original = binding(tmp_path, 9)
    existing = registry.register([original])[0][1]
    added = [binding(tmp_path, 1), binding(tmp_path, 2), binding(tmp_path, 3, shot=50)]
    result = registry.register(added)
    assert registry.lookup(original["registration_key"]) == (existing,)
    assert {instance for _, instance, _ in result if instance.startswith(original["shot_id"])} == {
        original["shot_id"]+"#0002", original["shot_id"]+"#0003"}
    assert not any(new for _, _, new in registry.register([original, *added]))


@pytest.mark.parametrize("change", [{"label": "hawk"}, {"field": "objects"}, {"legacy_json": "new/path.json"}])
def test_bound_identity_survives_changes(tmp_path, registry, change):
    original = binding(tmp_path)
    instance = registry.register([original])[0][1]
    assert registry.register([{**original, **change}])[0] == (original["registration_key"], instance, False)
    assert registry.database.execute("SELECT legacy_json FROM instances").fetchone() == (original["legacy_json"],)


def test_retirement_reintroduction_and_no_recycling(tmp_path, registry):
    source = binding(tmp_path)
    instance = registry.register([source])[0][1]
    registry.retire(source["registration_key"])
    assert registry.register([source])[0][1] == instance
    assert registry.database.execute("SELECT registration_state FROM instances").fetchone() == ("retired",)
    assert registry.register([binding(tmp_path, 2)])[0][1].endswith("#0002")
    with pytest.raises(sqlite3.IntegrityError):
        registry.database.execute("DELETE FROM instances")
    registry.database.rollback()


def test_interrupted_transaction_and_restart(tmp_path, registry):
    def interrupt(count):
        if count == 2:
            raise KeyboardInterrupt()
    records = [binding(tmp_path, number) for number in range(1, 4)]
    with pytest.raises(KeyboardInterrupt):
        registry.register(records, interrupt=interrupt)
    assert registry.database.execute("SELECT count(*) FROM instances").fetchone()[0] == 0
    assigned = registry.register(records)
    restarted = InstanceRegistry(tmp_path, registry.path)
    try:
        assert restarted.register(records) == [(key, instance, False) for key, instance, _ in assigned]
    finally:
        restarted.close()


def test_duplicate_binding_and_concurrent_registration(tmp_path, registry):
    def allocate(number):
        worker = InstanceRegistry(tmp_path, registry.path)
        try:
            return worker.register([binding(tmp_path, number)])[0]
        finally:
            worker.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(allocate, [1, 1, 2, 3, 4, 5]))
    assert len({instance for _, instance, _ in results}) == 5
    assert sum(new for _, _, new in results) == 5
    original = binding(tmp_path)
    with pytest.raises(ValueError):
        registry.register([{**original, "shot_id": build_shot_id("tmdb_61044", 50, 60)}])


def _process_allocate(arguments):
    project, path, number = arguments
    registry = InstanceRegistry(project, path)
    try:
        return registry.register([binding(project, number)])[0]
    finally:
        registry.close()


def test_separate_processes_serialize_ordinals(tmp_path, registry):
    with concurrent.futures.ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as executor:
        results = list(executor.map(_process_allocate, [(tmp_path, registry.path, number) for number in (1, 1, 2, 3, 4)]))
    assert len({instance for _, instance, _ in results}) == 4
    assert sum(new for _, _, new in results) == 4
    assert registry.database.execute("SELECT high_water FROM shots").fetchone() == (4,)


def test_registry_rejects_mismatched_media_without_caller_validation(tmp_path, registry):
    source = binding(tmp_path)
    with pytest.raises(ValueError):
        registry.register([{**source, "media_id": "tmdb_42"}])
    assert registry.database.execute("SELECT count(*) FROM instances").fetchone() == (0,)


def test_duplicate_original_path_cannot_get_second_binding(tmp_path, registry):
    source = binding(tmp_path)
    registry.register([source])
    with pytest.raises(sqlite3.IntegrityError):
        registry.register([{**source, "registration_key": "0"*64}])
    assert registry.database.execute("SELECT high_water FROM shots").fetchone() == (1,)


@pytest.mark.parametrize("label,slug", [("ammunition crate", "ammunition-crate"), ("brown_hat", "brown-hat"),
    (" Brown Hat ", "brown-hat"), ("checkered tablecloth", "checkered-tablecloth"),
    ("A / B \\ C", "a-b-c"), ("CAFÉ", "café"), (" Ｂｉｒｄ ", "bird"), ("***", "")])
def test_slugging(label, slug):
    assert label_slug(label) == slug


def test_collision_detection_and_shared_path(tmp_path, registry):
    assert label_slug("brown_hat") == label_slug("Brown Hat")
    assert label_slug("ammo belt") != label_slug("ammunition belt")
    instance = registry.register([binding(tmp_path)])[0][1]
    path = proposed_artifact_directory("silhouettes", "movie", "animals", "bird", instance)
    assert path == f"data/silhouettes/movie/animals/bird/{instance}"
    assert "/catalog/" not in path and "object_" not in path
    assert proposed_artifact_directory("engravings", "movie", "animals", "bird", instance).endswith(instance)
    assert instance_identifier(build_shot_id("tmdb_1", 1, 2), 10000).endswith("#10000")
    with pytest.raises(ValueError):
        proposed_artifact_directory("silhouettes", "movie", "../animals", "bird", instance)


@pytest.mark.parametrize("change", [{"media_id": ""}, {"shot_id": ""}, {"field": ""}, {"label": ""},
    {"shot_id": "tmdb_42@f000010-f000020"}, {"assignment": {"state": "inactive"}}])
def test_missing_components_and_lifecycle(tmp_path, change):
    record = {"media_type": "movie", "media_id": "tmdb_61044", "shot_id": build_shot_id("tmdb_61044", 10, 20),
              "field": "animals", "label": "bird", "filename_stem": "film", "frame": 15,
              "path": tmp_path / "data/silhouettes/catalog/movie/film/bird/object_0001.json"}
    with pytest.raises(ValueError):
        prepare_registration({**record, **change}, tmp_path)


def test_research_location_and_original_trace(tmp_path, registry):
    with pytest.raises(ValueError):
        InstanceRegistry(tmp_path, tmp_path / "data/silhouettes/registry.sqlite3")
    original = binding(tmp_path)
    registry.register([original])
    row = registry.database.execute("SELECT legacy_identity,legacy_json,legacy_png,media_id,shot_id FROM instances").fetchone()
    assert row == tuple(original[key] for key in ("legacy_identity", "legacy_json", "legacy_png", "media_id", "shot_id"))


def test_planner_accounting_collisions_and_readonly_inputs(tmp_path):
    import json
    from unittest.mock import patch
    from services.illustration_index import rebuild_index
    from services.silhouette_canonical_identity_research import (
        allocate_plan, analysis, build_destinations, create_plan, plan_slugs,
        reconcile_catalog_inventory, safety_snapshot, compare_safety, stage_active,
    )
    output = tmp_path / RESEARCH
    output.mkdir(parents=True)
    indexed = {"movie": [], "gameplay": []}
    for media, label, state, number in (("movie", "Brown Hat", "active", 1),
                                      ("movie", "brown_hat", "active", 2),
                                      ("movie", "bird", "inactive", 3),
                                      ("gameplay", "bird", "active", 1)):
        media_id = "tmdb_61044" if media == "movie" else "game_rdr2_ce5e0bba"
        path = tmp_path / f"data/silhouettes/catalog/{media}/film/{label}/object_{number:04d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"media_type": media, "media_id": media_id, "shot_id": build_shot_id(media_id, 10, 20),
                  "field": "wearing", "label": label, "frame": 15, "filename_stem": "film",
                  "assignment": {"state": state}}
        path.write_text(json.dumps(record))
        path.with_suffix(".png").write_bytes(b"not decoded")
        if state == "active":
            indexed[media].append({**record, "path": path})
    for media, records in indexed.items():
        with patch("services.illustration_index._scan_silhouettes", return_value=records):
            rebuild_index(tmp_path, "silhouettes", media)
    safety_snapshot(tmp_path, output/"before.sqlite3")
    database = create_plan(output/"plan.sqlite3")
    assert stage_active(tmp_path, database)["indexed_active"] == {"movie": 2, "gameplay": 1}
    assert reconcile_catalog_inventory(tmp_path, database, output/"before.sqlite3")["unindexed_catalog"] == 1
    collisions = plan_slugs(database)
    assert collisions == [{"base_slug": "brown-hat", "labels": ["Brown Hat", "brown_hat"]}]
    assert allocate_plan(tmp_path, database, output/"registry.sqlite3")["new_assignments"] == 3
    assert allocate_plan(tmp_path, database, output/"registry.sqlite3")["new_assignments"] == 0
    build_destinations(database)
    report = analysis(database)
    assert report["active_records_staged"] == 3
    assert report["excluded"] == [("inactive", 1)]
    assert report["identity_collisions"] == report["destination_file_collisions"] == 0
    assert database.execute("SELECT count(*) FROM file_mappings").fetchone()[0] == 6
    destinations = database.execute("SELECT registration_key,destination FROM records ORDER BY registration_key LIMIT 2").fetchall()
    with pytest.raises(sqlite3.IntegrityError):
        database.execute("UPDATE records SET destination=? WHERE registration_key=?", (destinations[0][1],destinations[1][0]))
    database.rollback()
    database.close()
    safety_snapshot(tmp_path, output/"after.sqlite3")
    assert compare_safety(output/"before.sqlite3", output/"after.sqlite3")["unchanged"]