import json
import sqlite3
import errno
import hashlib
import os
from pathlib import Path

import pytest

from data.resource_storage import digest
from services.resource_graph import transform_fixture_metadata
from services.resource_migration import _file, _transform, encode, execute_fixture_renames
from tool import project_access as access
from test_resource_rename import _plan


OUTPUTS = (
    "data/indexes/corpus_stats.json",
    "data/indexes/illustration/gameplay-engravings.sqlite3",
    "data/indexes/illustration/gameplay-silhouettes.revision",
    "data/indexes/illustration/gameplay-silhouettes.sqlite3",
    "data/indexes/illustration/movie-engravings.revision",
    "data/indexes/illustration/movie-engravings.sqlite3",
    "data/indexes/illustration/movie-silhouettes.revision",
    "data/indexes/illustration/movie-silhouettes.sqlite3",
    "data/indexes/viability/movie-silhouette-viability.sqlite3",
    "data/vocabulary/vocabulary-gameplay-derived.json",
    "data/vocabulary/vocabulary-gameplay.json",
    "data/vocabulary/vocabulary-movie-derived.json",
    "data/vocabulary/vocabulary-movie.json",
    "data/vocabulary/vocabulary_gameplay.json",
    "data/vocabulary/vocabulary_movie.json",
)


def _canonical_fixture(project, *, historical_questionable=False, first_record=None, first_values=None, first_image_bytes=None):
    from data import resource_locations as locations
    from data.resource_storage import ResourceStore, adopt_registry
    from services.silhouette_canonical_identity import InstanceRegistry, RESEARCH, prepare_registration
    from services.resource_migration import _schema, transform_silhouette
    from PIL import Image
    identities = InstanceRegistry(project, project / RESEARCH / "fixture-identities.sqlite3")
    records = []
    for media_type, media_id in (("movie", "tmdb_1"), ("movie", "tmdb_2"), ("movie", "tmdb_3"), ("gameplay", "game_fixture1"), ("gameplay", "game_fixture2"), ("gameplay", "game_fixture3")):
        media = {"filename": media_type + "-" + media_id + ".mp4", "media_id": media_id}
        metadata = project / "data/metadata" / (media_type + ".json")
        metadata.parent.mkdir(parents=True, exist_ok=True)
        previous = json.loads(metadata.read_text())["media"] if metadata.exists() else []
        metadata.write_text(json.dumps({"media": previous + [media]}))
        record = {**media, "media_type": media_type, "filename_stem": Path(media["filename"]).stem, "shot_id": media_id + "@f000010-f000020",
                  "frame": 12, "field": "objects", "label": "tree", "bbox": [0, 0, 2, 2], "frame_size": [2, 2], "mask_area": 4,
                  "sam_model": "fixture", "frame_match_model": "fixture", "source_frame": "frame:12", "unknown": {"retain": True}}
        if historical_questionable and not records:
            record.update(field="wearing", label="barber pants", search_provenance={
                "state": "questionable", "method": "canonical_search", "audit_version": "canonical-search-v1",
                "historical_search_label": "barber pants", "field": "wearing",
                "source_shot_identity": {"media_type": media_type, "filename": media["filename"], "shot_id": record["shot_id"]},
                "annotation_values": ["pants"], "canonical_search_match": False, "source_shot_returned": False,
            })
        if not records and first_record:
            record.update(first_record)
        legacy = project / "data/silhouettes/catalog" / media_type / Path(media["filename"]).stem / "tree/object_0001.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(record))
        Image.new("RGBA", (2, 2), (20, 40, 60, 255)).save(legacy.with_suffix(".png"))
        if not records and first_image_bytes is not None:
            legacy.with_suffix(".png").write_bytes(first_image_bytes)
        instance = identities.register([prepare_registration({**record, "assignment": {"state": "active"}, "path": legacy}, project)])[0][1]
        annotation = locations.annotation_path(project, media["filename"], media_type)
        annotation.parent.mkdir(parents=True, exist_ok=True)
        values = first_values if not records and first_values is not None else ["pants" if record["label"] == "barber pants" else "tree"]
        annotation.write_text(json.dumps([{"shot": {"shot_id": record["shot_id"], "annotation": {record["field"]: values}, "best_frame": {"frame": 14, "score": 0.9}}}]))
        records.append((record, instance, legacy, media))
    proposal = identities.path
    identities.close()
    preparation = project / "data/indexes/preparation.sqlite3"
    adopt_registry(project, proposal, preparation, exceptions=[], approved=True)
    plan = project / "plan.sqlite3"
    with sqlite3.connect(plan) as database:
        _schema(database)
        database.executemany("INSERT INTO meta VALUES(?,?)", [("project", json.dumps(str(project))), ("plan_revision", json.dumps({"stage": "2B.3d"})),
            ("identity_registry", json.dumps(str(preparation.relative_to(project)))), ("inputs", json.dumps({str(preparation.relative_to(project)): digest(preparation)}))])
        with sqlite3.connect(preparation) as bindings:
            database.executemany("INSERT INTO identity_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?)", bindings.execute("SELECT * FROM instances"))
            database.executemany("INSERT INTO shot_reservations VALUES(?,?)", bindings.execute("SELECT * FROM shots"))
        for record, instance, legacy, media in records:
            pair = locations.silhouette_destination(project, media, record["media_type"], instance, record["field"], record["label"])
            _file(database, project, str(legacy.relative_to(project)), pair["metadata"], "Transform", "services.resource_publication", instance_id=instance, hash_bytes=True)
            _file(database, project, str(legacy.with_suffix(".png").relative_to(project)), pair["image"], "Move", "services.resource_publication", instance_id=instance, hash_bytes=True)
            after = transform_silhouette(record, instance)
            _transform(database, str(legacy.relative_to(project)), "silhouette-v1", after, patch={key: after[key] for key in ("publication_contract", "resource_kind", "instance_id", "png")})
    return plan, digest(plan), preparation, records


def _full_fixture(project, *, historical_questionable=False, first_record=None):
    from data import resource_locations as locations
    from data.resource_storage import ResourceStore
    from services.frame_match import best_frame_path
    from services.resource_migration import _reference
    plan, _, preparation, records = _canonical_fixture(project, historical_questionable=historical_questionable, first_record=first_record)
    store = ResourceStore(project, preparation, approved=True)
    with sqlite3.connect(plan) as database:
        database.execute("ALTER TABLE collections ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        for record, instance, legacy, media in records:
            media_type = record["media_type"]
            annotation = locations.annotation_path(project, media["filename"], media_type)
            if media_type == "movie":
                store.commit_annotation_layout(media, media_type, "legacy-v1")
            source = str(annotation.relative_to(project))
            destination = locations.annotation_path(project, media["filename"], media_type, layout="media-first-v1")
            _file(database, project, source, destination, "Move", "data.annotate", hash_bytes=True)
            database.execute("INSERT INTO collections VALUES(?,?,?,?,0,1)", (media_type, media["media_id"], media["filename"], json.dumps({".annotations.json": {"source": source, "sha256": digest(annotation)}})))
            best = best_frame_path(str(project), media_type, media["filename"], record["shot_id"])
            best.parent.mkdir(parents=True, exist_ok=True)
            best.write_bytes(b"committed BEST bytes, not the query frame")
            _file(database, project, str(best.relative_to(project)), None, "Retain", "services.frame_match", hash_bytes=True)
            pair = locations.silhouette_destination(project, media, media_type, instance, record["field"], record["label"])
            _reference(database, str(legacy.relative_to(project)), "/png", str(legacy.with_suffix(".png").relative_to(project)), str(pair["image"].relative_to(project)), "path relocation")
        for reference in OUTPUTS:
            path = project / reference
            path.parent.mkdir(parents=True, exist_ok=True)
            if reference.endswith(".sqlite3"):
                with sqlite3.connect(path) as old:
                    old.execute("CREATE TABLE original(value TEXT)")
                    old.execute("INSERT INTO original VALUES('retained old output')")
            else:
                path.write_text("old-revision" if reference.endswith(".revision") else '{"old":true}')
            _file(database, project, reference, None, "Rebuild", "existing index owner", hash_bytes=True)
            database.execute("INSERT INTO index_transitions VALUES(?,?,?,?)", (reference, "existing index owner", "rebuild", "verified graph"))
        database.execute("UPDATE meta SET value=? WHERE key='inputs'", (json.dumps({str(preparation.relative_to(project)): digest(preparation)}),))
    return plan, digest(plan), preparation, records


def _fixture(project):
    plan, _, files = _plan(project)
    reference = "data/semantic.json"
    record = json.loads((project / reference).read_bytes())
    after = {**record, "publication_contract": "media-first-v1"}
    with sqlite3.connect(plan) as database:
        database.execute("DELETE FROM files WHERE source=?", (reference,))
        _file(database, project, reference, project / "data/semantic-new.json", "Transform", "fixture", hash_bytes=True)
        _transform(database, reference, "silhouette-v1", after, patch={"publication_contract": "media-first-v1"})
    return plan, digest(plan), files, record, after


def _execute(owner, plan, checksum, **kwargs):
    return transform_fixture_metadata(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True, **kwargs)


@pytest.mark.parametrize("boundary", [None, "metadata-prepared:synced", "metadata-preparation", "transform:intent:synced", "metadata-retained", "metadata-replaced", "transform:done:synced"])
def test_real_handoff_metadata_resume_and_rollback(boundary):
    with access.temporary_rename_project() as project:
        plan, checksum, files, record, after = _fixture(project)
        before = (project / "data/semantic.json").read_bytes()
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            if boundary:
                def stop(phase):
                    if phase == boundary:
                        raise RuntimeError("fixture interrupted")
                with pytest.raises(RuntimeError):
                    _execute(owner, plan, checksum, interrupt=stop)
            for _ in range(2):
                assert _execute(owner, plan, checksum)["state"] == "metadata_installed"
            assert (project / "data/semantic-new.json").read_bytes() == encode(after)
            assert not (project / "data/semantic.json").exists()
            for _ in range(2):
                assert _execute(owner, plan, checksum, action="rollback")["state"] == "metadata_rolled_back"
            assert (project / "data/semantic.json").read_bytes() == before
            assert all((project / destination).read_bytes() == payload for _, destination, payload, _ in files)
        assert not (project / "data/indexes/resources.sqlite3").exists()


def test_metadata_requires_completed_rename_handoff():
    with access.temporary_rename_project() as project:
        plan, checksum, _, _, _ = _fixture(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            with pytest.raises(sqlite3.OperationalError):
                _execute(owner, plan, checksum)
        assert (project / "data/semantic.json").exists()


@pytest.mark.parametrize("indexed_source", [False, True])
def test_import_moved_pairs_uses_canonical_validation_without_copying(indexed_source):
    from data.resource_storage import ResourceStore
    from services.resource_migration import readonly
    from services.resource_resolution import resolve_silhouette
    from contextlib import closing
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _canonical_fixture(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            _execute(owner, plan, checksum)
            with access.fixture_graph_access(owner), closing(readonly(plan)) as frozen:
                store = ResourceStore(project, preparation, approved=True)
                if indexed_source:
                    with pytest.raises(ValueError, match="Missing or ambiguous frozen pair"):
                        store.import_fixture_pair(owner, frozen, records[0][1], "silhouettes",
                                                  metadata_source=str(records[1][2].relative_to(project)))
                    for kind, mode in (("engravings", "frame"), ("silhouettes", "frame")):
                        with pytest.raises(ValueError, match="Missing or ambiguous frozen pair"):
                            store.import_fixture_pair(owner, frozen, records[0][1], kind, mode,
                                                      metadata_source=str(records[0][2].relative_to(project)))
                for record, instance, legacy, media in records:
                    queries = []
                    frozen.set_trace_callback(queries.append)
                    keywords = {"metadata_source": str(legacy.relative_to(project))} if indexed_source else {}
                    for _ in range(2):
                        assert store.import_fixture_pair(owner, frozen, instance, "silhouettes", **keywords) == 1
                    frozen.set_trace_callback(None)
                    if indexed_source:
                        lookup = next(query for query in queries if query.startswith("SELECT f.*,t.after_sha256"))
                        assert all("SCAN" not in row[3] for row in frozen.execute("EXPLAIN QUERY PLAN " + lookup))
                    resolved = resolve_silhouette(project, instance, registry=preparation)
                    assert resolved["record"]["shot_id"] == record["shot_id"]
                    assert resolved["record"]["unknown"] == {"retain": True}
                assert digest(plan) == checksum
            with pytest.raises(access.MaintenanceClosed):
                with access.operation(project):
                    pytest.fail("ordinary admission opened")


@pytest.mark.parametrize("assignment", [None, {"state": "active"},
    {"state": "active", "supersedes": [{"label": "wagon wheel", "object_id": "object_0004"}]}])
@pytest.mark.parametrize("boundary", [None, "registry-pair-before-commit", "registry-pair-after-commit"])
def test_legacy_active_import_with_curation_history_replays_without_override(monkeypatch, assignment, boundary):
    from contextlib import closing
    from data.resource_storage import ResourceStore
    from services.resource_migration import readonly
    from services.resource_publication import _validation
    from services.resource_resolution import resolve_silhouette
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _canonical_fixture(project, first_record={"assignment": assignment} if assignment else None)
        instance = records[0][1]
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            _execute(owner, plan, checksum)
            with access.fixture_graph_access(owner), closing(readonly(plan)) as frozen:
                store = ResourceStore(project, preparation, approved=True)
                def legacy_validation(owner, frozen, database, state, expected, metadata, binding, kind, mode):
                    return _validation(store, database, binding, metadata, kind, mode), None
                with monkeypatch.context() as legacy:
                    legacy.setattr(store, "_validate_fixture_pair", legacy_validation)
                    if boundary:
                        def stop(phase):
                            if phase == boundary:
                                with sqlite3.connect(preparation.as_uri() + "?mode=ro", uri=True) as snapshot:
                                    assert snapshot.execute("SELECT count(*) FROM resource_locations").fetchone()[0] == (1 if phase == "registry-pair-after-commit" else 0)
                                raise RuntimeError("legacy interrupted")
                        with pytest.raises(RuntimeError, match="legacy interrupted"):
                            store.import_fixture_pair(owner, frozen, instance, "silhouettes", interrupt=stop)
                    assert store.import_fixture_pair(owner, frozen, instance, "silhouettes") == 1
                committed = digest(preparation)
                for _ in range(2):
                    assert store.import_fixture_pair(owner, frozen, instance, "silhouettes") == 1
                    assert resolve_silhouette(project, instance, registry=preparation)["record"].get("assignment") == assignment
                    assert digest(preparation) == committed


def test_legacy_active_questionable_import_requires_explicit_assignment_transition(monkeypatch):
    from contextlib import closing
    from data.resource_storage import ResourceStore
    from services.resource_migration import readonly
    from services.resource_publication import _validation
    from services.resource_resolution import resolve_silhouette
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _canonical_fixture(project, historical_questionable=True, first_values=["barber pants"])
        instance = records[0][1]
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            _execute(owner, plan, checksum)
            with access.fixture_graph_access(owner), closing(readonly(plan)) as frozen:
                store = ResourceStore(project, preparation, approved=True)
                def legacy_validation(owner, frozen, database, state, expected, metadata, binding, kind, mode):
                    return _validation(store, database, binding, metadata, kind, mode), None
                with monkeypatch.context() as legacy:
                    legacy.setattr(store, "_validate_fixture_pair", legacy_validation)
                    assert store.import_fixture_pair(owner, frozen, instance, "silhouettes") == 1
                committed = digest(preparation)
                for _ in range(2):
                    with pytest.raises(ValueError, match="Conflicting existing location/import evidence"):
                        store.import_fixture_pair(owner, frozen, instance, "silhouettes")
                    for active_only in (True, False):
                        with pytest.raises(ValueError, match="lacks committed inactive assignment"):
                            resolve_silhouette(project, instance, registry=preparation, active_only=active_only)
                    assert digest(preparation) == committed


@pytest.mark.parametrize("corruption", ["record_path", "image_path", "record_sha256", "image_sha256", "expected_revision",
    "maintenance_operation", "assignment", "instance_id", "kind", "mode", "unfinished", "missing", "location_image", "location_revision"])
def test_completed_import_replay_refuses_contradictory_commitments(corruption):
    from contextlib import closing
    from data.resource_storage import ResourceStore
    from services.resource_migration import readonly
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _canonical_fixture(project)
        instance = records[0][1]
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            _execute(owner, plan, checksum)
            with access.fixture_graph_access(owner), closing(readonly(plan)) as frozen:
                store = ResourceStore(project, preparation, approved=True)
                assert store.import_fixture_pair(owner, frozen, instance, "silhouettes") == 1
                with sqlite3.connect(preparation) as database:
                    operation_id, payload = database.execute("SELECT operation_id,details FROM publication_operations").fetchone()
                    details = json.loads(payload)
                    if corruption in ("instance_id", "kind", "mode"):
                        replacement = {"instance_id": records[1][1], "kind": "engravings", "mode": "frame"}[corruption]
                        database.execute("UPDATE publication_operations SET " + corruption + "=?", (replacement,))
                    elif corruption == "unfinished":
                        database.execute("UPDATE publication_operations SET state='files_published'")
                    elif corruption == "missing":
                        database.execute("DELETE FROM publication_operations")
                    elif corruption == "location_image":
                        with pytest.raises(sqlite3.IntegrityError, match="Immutable ownership"):
                            database.execute("UPDATE resource_locations SET image_path=image_path||'.other'")
                        details["image_path"] += ".other"
                        database.execute("UPDATE publication_operations SET details=? WHERE operation_id=?", (json.dumps(details), operation_id))
                    elif corruption == "location_revision":
                        database.execute("UPDATE resource_locations SET revision=2")
                    else:
                        details[corruption] = {"expected_revision": 1, "assignment": {"state": "inactive"}}.get(corruption, "contradictory")
                        database.execute("UPDATE publication_operations SET details=? WHERE operation_id=?", (json.dumps(details), operation_id))
                contradictory = digest(preparation)
                for _ in range(2):
                    with pytest.raises(ValueError, match="Conflicting existing location/import evidence"):
                        store.import_fixture_pair(owner, frozen, instance, "silhouettes")
                    assert digest(preparation) == contradictory


@pytest.mark.parametrize("boundary", [None, "registry-pair-before-commit", "registry-pair-after-commit"])
def test_frozen_questionable_import_is_inactive_without_rewriting_history(boundary):
    from contextlib import closing
    from services.resource_migration import readonly
    from services.resource_resolution import resolve_silhouette
    from data.resource_storage import ResourceStore
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _canonical_fixture(project, historical_questionable=True)
        record, instance, legacy, media = records[0]
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            _execute(owner, plan, checksum)
            with access.fixture_graph_access(owner), closing(readonly(plan)) as frozen:
                store = ResourceStore(project, preparation, approved=True)
                target = frozen.execute("SELECT destination FROM files WHERE source=?", (str(legacy.relative_to(project)),)).fetchone()[0]
                before = (project / target).read_bytes()
                if boundary:
                    def stop(phase):
                        if phase == boundary:
                            with sqlite3.connect(preparation.as_uri() + "?mode=ro", uri=True) as snapshot:
                                count = snapshot.execute("SELECT count(*) FROM resource_locations").fetchone()[0]
                                assert count == (1 if phase == "registry-pair-after-commit" else 0)
                            if count:
                                assert resolve_silhouette(project, instance, registry=preparation, active_only=False)["record"]["assignment"]["state"] == "inactive"
                            raise RuntimeError("import interrupted")
                    with pytest.raises(RuntimeError, match="import interrupted"):
                        store.import_fixture_pair(owner, frozen, instance, "silhouettes", interrupt=stop)
                for _ in range(2):
                    assert store.import_fixture_pair(owner, frozen, instance, "silhouettes") == 1
                    with pytest.raises(ValueError, match="requires an active record"):
                        resolve_silhouette(project, instance, registry=preparation)
                    inspected = resolve_silhouette(project, instance, registry=preparation, active_only=False)
                    assert inspected["record"]["assignment"]["state"] == "inactive"
                    assert inspected["record"]["search_provenance"] == record["search_provenance"]
                    assert inspected["record"]["label"] == "barber pants"
                    assert (project / target).read_bytes() == before
                with sqlite3.connect(preparation) as database:
                    assert database.execute("SELECT count(*) FROM resource_locations").fetchone()[0] == 1
                    assert database.execute("SELECT count(*) FROM publication_operations").fetchone()[0] == 1


@pytest.mark.parametrize("questionable", [False, True])
def test_frozen_import_preserves_inactive_curation_history(questionable):
    from contextlib import closing
    from services.resource_migration import readonly
    from services.resource_resolution import resolve_silhouette
    from data.resource_storage import ResourceStore
    assignment = {"state": "inactive", "reason": "curator rejected", "recheck": {"state": "pending"}, "history": ["prior decision"]}
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _canonical_fixture(project, historical_questionable=questionable,
            first_record={"assignment": assignment, "human_best": True, "replacement_links": ["retained-reference"]})
        record, instance, legacy, media = records[0]
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            _execute(owner, plan, checksum)
            with access.fixture_graph_access(owner), closing(readonly(plan)) as frozen:
                store = ResourceStore(project, preparation, approved=True)
                for _ in range(2):
                    assert store.import_fixture_pair(owner, frozen, instance, "silhouettes") == 1
                    current = resolve_silhouette(project, instance, registry=preparation, active_only=False)["record"]
                    assert current["assignment"] == assignment and current["human_best"] is True
                    assert current["replacement_links"] == ["retained-reference"]


@pytest.mark.parametrize("attack", ["flag_only", "missing_provenance", "unverifiable_provenance", "self_declared_valid", "bad_context", "source_shot", "ambiguous_source", "metadata_hash", "png_hash", "image_integrity", "mapping", "identity", "geometry", "missing_field", "forged_plan"])
def test_questionable_import_does_not_bypass_other_validation(attack):
    from contextlib import closing
    from data import resource_locations as locations
    from data.resource_storage import ResourceStore
    from services.resource_migration import readonly
    changes = {"flag_only": {"search_provenance": {"state": "questionable"}},
               "missing_provenance": {"search_provenance": None},
               "unverifiable_provenance": {"search_provenance": {"state": "unverifiable"}},
               "self_declared_valid": {"search_provenance": {"state": "valid"}},
               "bad_context": {"search_provenance": {"state": "questionable", "method": "canonical_search",
                   "audit_version": "canonical-search-v1", "historical_search_label": "barber pants", "field": "wearing",
                   "source_shot_identity": {"media_type": "movie", "filename": "movie-tmdb_1.mp4", "shot_id": "tmdb_1@f000030-f000040"},
                   "annotation_values": ["pants"], "canonical_search_match": False, "source_shot_returned": False}},
               "geometry": {"bbox": [10, 10, 2, 2]}, "missing_field": {"sam_model": None}}
    with access.temporary_rename_project() as project:
        plan, _, preparation, records = _canonical_fixture(project, historical_questionable=True, first_record=changes.get(attack),
            first_image_bytes=b"not a PNG despite a matching frozen hash" if attack == "image_integrity" else None)
        record, instance, legacy, media = records[0]
        if attack == "identity":
            with sqlite3.connect(plan) as database:
                from services.resource_migration import transform_silhouette
                after = transform_silhouette(record, instance)
                after["instance_id"] = "tmdb_2@f000010-f000020#0001"
                database.execute("DELETE FROM transformations WHERE source=?", (str(legacy.relative_to(project)),))
                _transform(database, str(legacy.relative_to(project)), "silhouette-v1", after,
                           patch={key: after[key] for key in ("publication_contract", "resource_kind", "instance_id", "png")})
        checksum = digest(plan)
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            _execute(owner, plan, checksum)
            pair = locations.silhouette_destination(project, media, "movie", instance, record["field"], record["label"])
            if attack == "metadata_hash":
                pair["metadata"].write_text(pair["metadata"].read_text() + " ")
            if attack == "png_hash":
                pair["image"].write_bytes(b"corrupt PNG")
            if attack == "source_shot":
                locations.annotation_path(project, media["filename"], "movie", layout="legacy-v1").write_text("[]")
            if attack == "ambiguous_source":
                annotation = locations.annotation_path(project, media["filename"], "movie", layout="legacy-v1")
                entries = json.loads(annotation.read_text())
                annotation.write_text(json.dumps(entries + entries))
            if attack in ("mapping", "forged_plan"):
                with sqlite3.connect(plan) as database:
                    if attack == "mapping":
                        database.execute("UPDATE files SET destination=destination||'.wrong' WHERE source=?", (str(legacy.with_suffix('.png').relative_to(project)),))
                    else:
                        database.execute("INSERT INTO meta VALUES('forged', 'true')")
            with access.fixture_graph_access(owner), closing(readonly(plan)) as frozen:
                store = ResourceStore(project, preparation, approved=True)
                with pytest.raises((ValueError, OSError)):
                    store.import_fixture_pair(owner, frozen, instance, "silhouettes")
                with sqlite3.connect(preparation) as database:
                    assert database.execute("SELECT count(*) FROM resource_locations").fetchone()[0] == 0
                    assert database.execute("SELECT count(*) FROM publication_operations").fetchone()[0] == 0


@pytest.mark.parametrize("current_matches", [False, True])
def test_historical_inactive_discovery_and_explicit_inspection(current_matches, monkeypatch, capsys):
    from contextlib import closing
    import cli
    from mcp_server import mcp_server as server
    from data.resource_storage import ResourceStore
    from services.illustration_index import rebuild_index, query_page
    from services.resource_migration import readonly
    from services.resource_publication import publish_silhouette
    from services.resource_resolution import resolve_silhouette, resolve_source_silhouette, resolve_index_record, indexed_publications
    from services.silhouette_discovery import list_active_candidate_references
    from visualizers.components.illustration_source import SilhouetteSource
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _canonical_fixture(project, historical_questionable=True,
            first_values=["barber pants"] if current_matches else None)
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            _execute(owner, plan, checksum)
            with access.fixture_graph_access(owner), closing(readonly(plan)) as frozen:
                store = ResourceStore(project, preparation, approved=True)
                for record, instance, legacy, media in records:
                    if record["label"] == "barber pants" and not current_matches:
                        target = project / frozen.execute("SELECT destination FROM files WHERE source=?", (str(legacy.relative_to(project)),)).fetchone()[0]
                        with pytest.raises(ValueError, match="association mismatch"):
                            publish_silhouette(project, json.loads(target.read_text()), target.with_suffix('.png'), source_path=legacy)
                    store.import_fixture_pair(owner, frozen, instance, "silhouettes")
                for record, instance, legacy, media in records[1:]:
                    assert resolve_silhouette(project, instance, registry=preparation)["record"].get("assignment", {"state": "active"})["state"] == "active"
                assert rebuild_index(project, "silhouettes", "movie", registry=preparation)["count"] == 3
                assert query_page(project, "silhouettes", "movie")["total"] == 2
                administrative = query_page(project, "silhouettes", "movie", assignment_state="inactive")
                assert administrative["total"] == 1 and len(administrative["records"]) == 1
                inactive = administrative["records"][0]
                assert inactive["search_provenance"]["state"] == "questionable"
                assert inactive["instance_id"] == records[0][1]
                assert len(indexed_publications(project, "silhouettes", "movie", registry=preparation)) == 3
                assert list_active_candidate_references(project, word="barber pants", field="wearing")["entries"] == []
                monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: str(project) if key == "path" else default)
                monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: pytest.fail("discovery changed preferences"))
                monkeypatch.setattr(server, "_ctx", lambda: (str(project), "movie"))
                assert json.loads(server.list_silhouette_candidates("barber pants", field="wearing"))["entries"] == []
                assert json.loads(server.list_silhouettes("barber pants", field="wearing"))["entries"] == []
                args = cli.build_parser().parse_args(["index", "silhouette", "list", "barber pants", "--field", "wearing", "--json"])
                args.func(args)
                assert json.loads(capsys.readouterr().out)["entries"] == []
                source = SilhouetteSource(str(project))
                assert source.page(field="wearing")["records"] == []
                assert source.records(field="wearing") == []
                with pytest.raises(ValueError, match="requires an active record"):
                    resolve_source_silhouette(project, registry=preparation, instance_id=records[0][1])
                with pytest.raises(ValueError, match="requires an active record"):
                    source.thumbnail_path(inactive)
                with pytest.raises(ValueError, match="requires an active record"):
                    resolve_index_record(project, inactive)


@pytest.mark.parametrize("tamper", ["missing_assignment", "active_assignment", "wrong_hash", "missing_operation", "unfinished_operation", "stale_revision", "missing_registry_marker"])
def test_historical_assignment_corruption_fails_closed(tamper):
    from contextlib import closing
    from data.resource_storage import ResourceStore
    from services.resource_migration import readonly
    from services.resource_resolution import resolve_silhouette
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _canonical_fixture(project, historical_questionable=True)
        instance = records[0][1]
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            _execute(owner, plan, checksum)
            with access.fixture_graph_access(owner), closing(readonly(plan)) as frozen:
                ResourceStore(project, preparation, approved=True).import_fixture_pair(owner, frozen, instance, "silhouettes")
                with sqlite3.connect(preparation) as database:
                    details = json.loads(database.execute("SELECT details FROM publication_operations").fetchone()[0])
                    if tamper == "missing_assignment":
                        details.pop("assignment")
                    elif tamper == "active_assignment":
                        details["assignment"]["state"] = "active"
                    elif tamper == "wrong_hash":
                        details["image_sha256"] = "0" * 64
                    elif tamper == "stale_revision":
                        details["expected_revision"] = 2
                    database.execute("UPDATE publication_operations SET details=?", (json.dumps(details),))
                    if tamper == "missing_operation":
                        database.execute("DELETE FROM publication_operations")
                    elif tamper == "unfinished_operation":
                        database.execute("UPDATE publication_operations SET state='prepared'")
                    elif tamper == "missing_registry_marker":
                        database.execute("DELETE FROM meta WHERE key='historical_import_operation'")
                with pytest.raises(ValueError):
                    resolve_silhouette(project, instance, registry=preparation, active_only=False)


def test_complete_graph_recovers_legacy_active_import_history(monkeypatch):
    from data.resource_storage import ResourceStore
    from services.resource_graph import install_fixture_graph
    from services.resource_publication import _validation
    from services.resource_resolution import resolve_silhouette
    assignment = {"state": "active", "supersedes": [{"label": "wagon wheel", "object_id": "object_0004"}]}
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _full_fixture(project, first_record={"assignment": assignment})
        preparation_before = preparation.read_bytes()
        instance = records[0][1]
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
            candidate = project / "data/indexes/.graph-graph.sqlite3"
            def legacy_validation(store, owner, frozen, database, state, expected, metadata, binding, kind, mode):
                return _validation(store, database, binding, metadata, kind, mode), None
            def stop(phase):
                if phase == "registry-pair-after-commit":
                    with sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True) as database:
                        payload = database.execute("SELECT details FROM publication_operations WHERE instance_id=?", (instance,)).fetchone()
                    if payload:
                        assert "assignment" not in json.loads(payload[0])
                        raise RuntimeError("legacy graph interrupted")
            with monkeypatch.context() as legacy:
                legacy.setattr(ResourceStore, "_validate_fixture_pair", legacy_validation)
                with pytest.raises(RuntimeError, match="legacy graph interrupted"):
                    install_fixture_graph(owner, plan, **arguments, interrupt=stop)
            for _ in range(2):
                assert install_fixture_graph(owner, plan, **arguments)["graph_accepted"]
                registry = project / "data/indexes/resources.sqlite3"
                with access.fixture_graph_access(owner):
                    assert resolve_silhouette(project, instance, registry=registry)["record"]["assignment"] == assignment
                with sqlite3.connect(registry) as database:
                    assert database.execute("SELECT count(*) FROM resource_locations").fetchone()[0] == 6
                    details = json.loads(database.execute("SELECT details FROM publication_operations WHERE instance_id=?", (instance,)).fetchone()[0])
                    assert "assignment" not in details
            assert preparation.read_bytes() == preparation_before
            assert all((project / reference).is_file() for reference in OUTPUTS)


@pytest.mark.parametrize("boundary", [None, "registry-pair-before-commit", "registry-pair-after-commit"])
def test_complete_questionable_graph_preserves_history_on_resume(boundary):
    from services.resource_graph import install_fixture_graph
    from services.resource_resolution import resolve_silhouette
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _full_fixture(project, historical_questionable=True)
        preparation_before = preparation.read_bytes()
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
            if boundary:
                def stop(phase):
                    if phase == boundary:
                        raise RuntimeError("questionable graph interrupted")
                with pytest.raises(RuntimeError, match="questionable graph interrupted"):
                    install_fixture_graph(owner, plan, **arguments, interrupt=stop)
            for _ in range(2):
                result = install_fixture_graph(owner, plan, **arguments)
                assert result["graph_accepted"]
                registry = project / "data/indexes/resources.sqlite3"
                with access.fixture_graph_access(owner):
                    inspected = resolve_silhouette(project, records[0][1], registry=registry, active_only=False)
                    assert inspected["record"]["assignment"]["state"] == "inactive"
                    assert inspected["record"]["search_provenance"] == records[0][0]["search_provenance"]
                    stored = json.loads(inspected["metadata"].read_text())
                    assert stored["label"] == "barber pants" and "assignment" not in stored
                    assert resolve_silhouette(project, records[1][1], registry=registry)["record"]["label"] == "tree"
            assert preparation.read_bytes() == preparation_before
            assert all((project / reference).is_file() for reference in OUTPUTS)
            with sqlite3.connect(registry) as database:
                assert database.execute("SELECT count(*) FROM resource_locations").fetchone()[0] == 6
                assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("boundary", [None, "registry-intent:synced", "registry-preparation", "registry-pair-committed", "registry-prepared:synced", "registry-installation", "registry-installed", "registry-completion:synced"])
def test_registry_snapshot_preserves_preparation_and_canonical_bindings(boundary):
    from services.resource_graph import install_fixture_registry
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _canonical_fixture(project)
        before = preparation.read_bytes()
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            _execute(owner, plan, checksum)
            if boundary:
                def stop(phase):
                    if phase == boundary:
                        raise RuntimeError("registry interruption")
                with pytest.raises(RuntimeError):
                    install_fixture_registry(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True, interrupt=stop)
            for _ in range(2):
                result = install_fixture_registry(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
                assert result["graph_accepted"] is False
            with sqlite3.connect(result["registry"]) as database:
                assert database.execute("SELECT count(*) FROM resource_locations").fetchone()[0] == len(records)
                assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert preparation.read_bytes() == before


def test_complete_fifteen_output_graph_and_explicit_reversal():
    from services.resource_graph import install_fixture_graph, rollback_fixture_graph
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, records = _full_fixture(project)
        baseline = {reference: (project / reference).read_bytes() for reference in OUTPUTS}
        original_registry = preparation.read_bytes()
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            for _ in range(2):
                result = install_fixture_graph(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
                assert result["graph_accepted"] and not result["ordinary_consumers_enabled"]
            assert preparation.read_bytes() == original_registry
            for _ in range(2):
                result = rollback_fixture_graph(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
                assert result["task3_reversed"] and result["task2_rollback_required"]
            assert not (project / "data/indexes/resources.sqlite3").exists()
            assert all((project / reference).read_bytes() == payload for reference, payload in baseline.items())
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True, action="rollback")
            assert all(legacy.exists() and legacy.with_suffix(".png").exists() for _, _, legacy, _ in records)


@pytest.mark.parametrize("boundary", ["metadata-retained", "metadata-replaced", "registry-prepared:synced", "registry-installed", "index-retain-intent:synced", "index-retained:synced", "index-built:synced", "index-replacement", "index-installed:synced", "graph-validation", "final-acceptance:before-sync", "final-acceptance:synced"])
def test_complete_graph_interruption_resume(boundary):
    from services.resource_graph import install_fixture_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _full_fixture(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)
            def stop(phase):
                if phase == boundary:
                    raise RuntimeError("graph interruption")
            with pytest.raises(RuntimeError):
                install_fixture_graph(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True, interrupt=stop)
            assert install_fixture_graph(owner, plan, approved_digest=checksum, plan_identity="2B.3d", clients_stopped=True)["graph_accepted"]


@pytest.mark.parametrize("kind,category", [("missing", "missing_resource"), ("changed", "changed_content"), ("extra", "unexpected_resource"), ("index", "invalid_index"), ("identity", "incorrect_identity")])
def test_validator_rejects_broken_complete_relationship(kind, category):
    from services.resource_graph import install_fixture_graph, validate_fixture_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, records = _full_fixture(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            install = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
            execute_fixture_renames(owner, plan, **install)
            install_fixture_graph(owner, plan, **install)
            if kind in ("missing", "changed", "extra"):
                from data import resource_locations as locations
                record, instance, _, media = records[0]
                with access.fixture_graph_access(owner):
                    image = locations.silhouette_destination(project, media, "movie", instance, "objects", "tree")["image"]
                if kind == "missing":
                    image.unlink()
                elif kind == "changed":
                    image.write_bytes(b"changed PNG")
                else:
                    image.with_name("unapproved.png").write_bytes(b"extra")
            elif kind == "index":
                (project / "data/indexes/illustration/movie-silhouettes.sqlite3").write_bytes(b"corrupt")
            else:
                with sqlite3.connect(project / "data/indexes/resources.sqlite3") as registry:
                    registry.execute("UPDATE shots SET high_water=high_water+1")
            result = validate_fixture_graph(owner, plan, **install)
            assert not result["graph_accepted"]
            assert category in {item["category"] for item in result["failures"]}


def _engraving_fixture(project, base_factory=_full_fixture):
    from data import resource_locations as locations
    from services.resource_migration import transform_engraving, _reference
    from services.frame_match import best_frame_path
    from PIL import Image
    plan, _, preparation, records = base_factory(project)
    record, instance, _, media = records[0]
    source_pair = locations.silhouette_destination(project, media, "movie", instance, "objects", "tree")
    pair = locations.engraving_destination(project, media, "movie", instance, "objects", "tree", "frame")
    run_directory = locations.generation_run_destination(project, media, "movie", instance, "objects", "tree", "local", 1)
    legacy = project / "data/engravings/catalog/movie/fixture/tree/object_0001/frame/engraving.json"
    provider = legacy.parent / "local/provider-run.json"
    provider.parent.mkdir(parents=True)
    provider.write_text('{"historical":"provider evidence, unchanged"}')
    generation = {"service": "qwen-local", "model": "fixture", "model_version": "1", "prompt": "tree", "seed": 42, "parameters": {}, "software": {"python": "fixture"}}
    best = best_frame_path(str(project), "movie", media["filename"], record["shot_id"])
    source = {"image_sha256": digest(project / (str(records[0][2].with_suffix(".png").relative_to(project)))), "mask_association": "silhouette-alpha",
              "evidence": [{"media_id": media["media_id"], "frame": 14, "image_sha256": digest(best), "query_specific_frame": 12}]}
    payload = {"source_instance_id": instance, "mode": "frame", "generation": generation, "source": source}
    artifacts = {"provider-run.json": digest(provider)}
    wrapper = json.dumps({**payload, "artifacts": artifacts}, sort_keys=True, allow_nan=False).encode()
    original = {"schema_version": 2, "status": "generated", "mode": "frame", "source": {"silhouette_json": str(records[0][2].relative_to(project)), "silhouette_png": str(records[0][2].with_suffix(".png").relative_to(project))},
                "generation": generation, "output_png": str(legacy.with_suffix(".png")), "provenance": {"run_json": str(provider.relative_to(project))}, "review_state": "needs_review", "historical": {"preserve": True}}
    legacy.write_text(json.dumps(original))
    Image.new("RGB", (2, 2), (70, 90, 110)).save(legacy.with_suffix(".png"))
    after = transform_engraving(original, record, instance, source_pair, pair, str((run_directory / "run.json").relative_to(project)), generation=generation, canonical_source=source, published_run={"provider": "local", "number": 1, "sha256": hashlib.sha256(wrapper).hexdigest()})
    run_plan = {"instance_id": instance, "provider": "local", "proposed_number": 1, "require_next_reservation_number": 1,
                "mode": "frame", "destination": str((run_directory / "run.json").relative_to(project)), "payload": payload,
                "artifacts": {"provider-run.json": str(provider.relative_to(project))}, "artifact_hashes": artifacts,
                "wrapper_sha256": hashlib.sha256(wrapper).hexdigest(), "wrapper_bytes": len(wrapper)}
    with sqlite3.connect(plan) as database:
        _file(database, project, str(legacy.relative_to(project)), pair["metadata"], "Transform", "services.resource_publication", instance_id=instance, mode="frame", hash_bytes=True)
        _file(database, project, str(legacy.with_suffix(".png").relative_to(project)), pair["image"], "Move", "services.resource_publication", instance_id=instance, mode="frame", hash_bytes=True)
        _file(database, project, str(provider.relative_to(project)), run_directory / "provider-run.json", "Move", "services.resource_publication", instance_id=instance, mode="frame", hash_bytes=True)
        _transform(database, str(legacy.relative_to(project)), "engraving-v2", after)
        database.execute("INSERT INTO meta VALUES('generation_run_plan',?)", (json.dumps(run_plan),))
        _reference(database, str(legacy.relative_to(project)), "/source/silhouette_json", original["source"]["silhouette_json"], str(source_pair["metadata"].relative_to(project)), "identity-based source relationship")
    return plan, digest(plan), preparation, records


@pytest.mark.parametrize("boundary", [None, "historical-run-reserved", "historical-run-installed"])
def test_historical_run_import_preserves_mode_best_and_reservation(boundary):
    from services.resource_graph import install_fixture_graph
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, _ = _engraving_fixture(project)
        before = preparation.read_bytes()
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
            execute_fixture_renames(owner, plan, **arguments)
            if boundary:
                def stop(phase):
                    if phase == boundary:
                        raise RuntimeError("run interrupted")
                with pytest.raises(RuntimeError):
                    install_fixture_graph(owner, plan, **arguments, interrupt=stop)
            assert install_fixture_graph(owner, plan, **arguments)["graph_accepted"]
            with sqlite3.connect(project / "data/indexes/resources.sqlite3") as database:
                assert database.execute("SELECT number,state,mode FROM generation_runs").fetchall() == [(1, "complete", "frame")]
            assert preparation.read_bytes() == before


@pytest.mark.parametrize("boundary", ["metadata-preparation", "transform:intent:synced", "registry-preparation", "registry-installed", "index-retained:synced", "index-built:synced", "final-acceptance:before-sync"])
@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES])
def test_graph_filesystem_error_never_accepts_and_safe_boundaries_resume(boundary, code):
    from services.resource_graph import install_fixture_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _full_fixture(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
            execute_fixture_renames(owner, plan, **arguments)
            def fail(phase):
                if phase == boundary:
                    raise OSError(code, "fixture graph filesystem failure")
            with pytest.raises(OSError):
                install_fixture_graph(owner, plan, **arguments, interrupt=fail)
            assert install_fixture_graph(owner, plan, **arguments)["graph_accepted"]


@pytest.mark.parametrize("boundary", ["rollback-index-retired", "rollback-index:synced", "rollback-registry-retired", "rollback-registry:synced", "metadata-uninstalled", "transform:undone:synced"])
def test_interrupted_graph_reversal_repeats_safely(boundary):
    from services.resource_graph import install_fixture_graph, rollback_fixture_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _full_fixture(project)
        baseline = {reference: (project / reference).read_bytes() for reference in OUTPUTS}
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
            execute_fixture_renames(owner, plan, **arguments)
            install_fixture_graph(owner, plan, **arguments)
            def stop(phase):
                if phase == boundary:
                    raise RuntimeError("rollback interrupted")
            with pytest.raises(RuntimeError):
                rollback_fixture_graph(owner, plan, **arguments, interrupt=stop)
            assert rollback_fixture_graph(owner, plan, **arguments)["task3_reversed"]
            assert all((project / reference).read_bytes() == payload for reference, payload in baseline.items())


def _member_fixture(project):
    import numpy as np
    from data import resource_locations as locations
    from services.resource_migration import transform_manifest, _reference
    plan, _, preparation, records = _full_fixture(project)
    with sqlite3.connect(plan) as database:
        database.row_factory = sqlite3.Row
        for collection in database.execute("SELECT * FROM collections").fetchall():
            media_type, filename = collection["media_type"], collection["filename"]
            artifacts = json.loads(collection["artifacts"])
            mapping = {}
            for suffix in (".annotations.json", ".annotations.npy", ".frames.npy", ".frames.valid.npy", ".annotations.txt", ".log"):
                source = locations.annotation_path(project, filename, media_type, suffix)
                destination = locations.annotation_path(project, filename, media_type, suffix, layout="media-first-v1")
                if suffix.endswith(".npy"):
                    np.save(source, np.array([True], dtype=bool) if suffix.endswith(".valid.npy") else np.array([[1, 2]], dtype=np.float32), allow_pickle=False)
                elif suffix != ".annotations.json":
                    source.write_text("unknown historical log/text retained")
                if suffix != ".annotations.json":
                    _file(database, project, str(source.relative_to(project)), destination, "Move", "data.annotate", hash_bytes=True)
                mapping[str(source.relative_to(project))] = str(destination.relative_to(project))
                artifacts[suffix] = {"source": str(source.relative_to(project)), "sha256": digest(source)}
            for prefix in ("annotations", "frames"):
                suffix = "." + prefix + ".manifest.json"
                source = locations.annotation_path(project, filename, media_type, suffix)
                destination = locations.annotation_path(project, filename, media_type, suffix, layout="media-first-v1")
                manifest = {"version": 1, "index_type": "annotation-embeddings" if prefix == "annotations" else "frame-embeddings", "source_hash": "intentionally-stale-do-not-bless", "unknown": {"retain": True}}
                for section, artifact_suffix in (("json", ".annotations.json"), ("npy", "." + prefix + ".npy")):
                    manifest[section] = {"path": artifacts[artifact_suffix]["source"], "sha256": "historical-stale-value"}
                source.write_text(json.dumps(manifest))
                _file(database, project, str(source.relative_to(project)), destination, "Transform", "data.annotate", hash_bytes=True)
                _transform(database, str(source.relative_to(project)), "manifest-v1", transform_manifest(manifest, mapping))
                for section in ("json", "npy"):
                    _reference(database, str(source.relative_to(project)), "/" + section + "/path", manifest[section]["path"], mapping[manifest[section]["path"]], "approved manifest path relocation")
                artifacts[suffix] = {"source": str(source.relative_to(project)), "sha256": digest(source)}
            database.execute("UPDATE collections SET artifacts=?,stale_manifests=2 WHERE media_type=? AND media_id=?", (json.dumps(artifacts), media_type, collection["media_id"]))
    return plan, digest(plan), preparation, records


def test_complete_optional_collection_members_and_stale_manifests():
    from services.resource_graph import install_fixture_graph
    from data import resource_locations as locations
    with access.temporary_rename_project() as project:
        plan, checksum, _, records = _member_fixture(project)
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
            execute_fixture_renames(owner, plan, **arguments)
            assert install_fixture_graph(owner, plan, **arguments)["graph_accepted"]
            with access.fixture_graph_access(owner):
                for record, _, _, media in records:
                    path = locations.annotation_path(project, media["filename"], record["media_type"], ".frames.manifest.json", registry=project / "data/indexes/resources.sqlite3")
                    manifest = json.loads(path.read_text())
                    assert manifest["source_hash"] == "intentionally-stale-do-not-bless"
                    assert manifest["unknown"] == {"retain": True}


@pytest.mark.parametrize("boundary", ["metadata-replaced", "registry-snapshot-created", "registry-installed", "index-staged", "index-installed:synced", "graph-validation", "final-acceptance:before-sync", "context-installed"])
def test_real_process_exit_graph_recovery(boundary, tmp_path, monkeypatch):
    from services import maintenance_preflight
    from services.resource_graph import install_fixture_graph
    processes = tmp_path / "isolated-proc"
    processes.mkdir()
    monkeypatch.setattr(maintenance_preflight, "_PROC_ROOT", processes)
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _full_fixture(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
        child = os.fork()
        if child == 0:
            try:
                def stop(phase):
                    if phase == boundary:
                        os._exit(73)
                with access.maintenance(project, "graph", approved=True) as owner:
                    install_fixture_graph(owner, plan, **arguments, interrupt=stop)
            finally:
                os._exit(91)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 73
        with pytest.raises(access.MaintenanceClosed):
            with access.operation(project):
                pytest.fail("process exit reopened project")
        with access.maintenance(project, "graph", approved=True) as owner:
            assert install_fixture_graph(owner, plan, **arguments)["graph_accepted"]


def test_unsealed_index_owner_failure_before_output_resumes(monkeypatch):
    from services import resource_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _full_fixture(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            def fail(*args):
                raise OSError(errno.ENOSPC, "index builder failed before output sealing")
            with monkeypatch.context() as patch:
                patch.setattr(resource_graph, "_build_index", fail)
                with pytest.raises(OSError):
                    resource_graph.install_fixture_graph(owner, plan, **arguments)
            assert resource_graph.install_fixture_graph(owner, plan, **arguments)["graph_accepted"]
            assert resource_graph.rollback_fixture_graph(owner, plan, **arguments)["task3_reversed"]


@pytest.mark.parametrize("boundary", [None, "rollback-run-intent:synced", "rollback-run:synced"])
def test_historical_run_reversal_retains_wrapper_and_allows_separate_task2_rollback(boundary):
    from services.resource_graph import install_fixture_graph, rollback_fixture_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _engraving_fixture(project)
        with sqlite3.connect(plan) as database:
            run = json.loads(database.execute("SELECT value FROM meta WHERE key='generation_run_plan'").fetchone()[0])
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            install_fixture_graph(owner, plan, **arguments)
            if boundary:
                def stop(phase):
                    if phase == boundary:
                        raise RuntimeError("wrapper rollback interrupted")
                with pytest.raises(RuntimeError):
                    rollback_fixture_graph(owner, plan, **arguments, interrupt=stop)
            assert rollback_fixture_graph(owner, plan, **arguments)["task3_reversed"]
            assert not (project / run["destination"]).exists()
            assert execute_fixture_renames(owner, plan, **arguments, action="rollback")["state"] == "rolled_back"


@pytest.mark.parametrize("kind", ["expiry", "thread", "async-task", "fake-owner"])
def test_fixture_graph_scope_is_not_transferable(kind):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    with access.temporary_rename_project() as project:
        access.enroll(project, approved=True)
        with access.maintenance(project, "graph", approved=True) as owner:
            def read():
                with access.operation(project):
                    return True
            if kind == "fake-owner":
                class FakeOwner:
                    def require_fixture_renames(self):
                        return {"project": str(project)}
                with pytest.raises(access.MaintenanceError):
                    with access.fixture_graph_access(FakeOwner()):
                        pytest.fail("fake capability accepted")
            else:
                with access.fixture_graph_access(owner):
                    assert read()
                    if kind == "thread":
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            with pytest.raises(access.MaintenanceError):
                                pool.submit(read).result()
                    if kind == "async-task":
                        async def child():
                            return read()
                        with pytest.raises(access.MaintenanceError):
                            asyncio.run(child())
                with pytest.raises(access.MaintenanceClosed):
                    read()
        with pytest.raises(access.MaintenanceError):
            with access.fixture_graph_access(owner):
                pytest.fail("expired owner accepted")


@pytest.mark.parametrize("reference", ["../escape", ".crossing-maintenance/link/absent"])
def test_recovery_paths_reject_escape_and_symlinked_absent_leaf(reference, tmp_path):
    from services.resource_graph import _exact
    with access.temporary_rename_project() as project:
        control = project / access.CONTROL
        control.mkdir(exist_ok=True)
        (control / "link").symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(ValueError):
            _exact(project, reference, "0" * 64)
        assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("kind", ["missing-manifest", "changed-array", "missing-best", "changed-best"])
def test_incomplete_or_changed_collection_and_best_never_accepts(kind):
    from services.resource_graph import install_fixture_graph, validate_fixture_graph
    with access.temporary_rename_project() as project:
        factory = _member_fixture if kind in ("missing-manifest", "changed-array") else _engraving_fixture
        plan, checksum, _, _ = factory(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            with sqlite3.connect(plan) as database:
                if kind == "missing-manifest":
                    reference = database.execute("SELECT source FROM transformations WHERE schema='manifest-v1' LIMIT 1").fetchone()[0]
                elif kind == "changed-array":
                    reference = database.execute("SELECT destination FROM files WHERE source LIKE '%.frames.npy' LIMIT 1").fetchone()[0]
                else:
                    reference = database.execute("SELECT source FROM files WHERE disposition='Retain' AND owner='services.frame_match' LIMIT 1").fetchone()[0]
            if kind.startswith("missing"):
                (project / reference).unlink()
            else:
                (project / reference).write_bytes(b"corrupt retained or moved bytes")
            with pytest.raises((ValueError, OSError)):
                install_fixture_graph(owner, plan, **arguments)
            assert not validate_fixture_graph(owner, plan, **arguments)["graph_accepted"]


def test_fixture_storage_accounting_reports_measured_peak():
    from services.resource_graph import install_fixture_graph, rollback_fixture_graph
    def allocation(project):
        blocks = 0
        logical = 0
        inodes = 0
        for parent, directories, filenames in os.walk(project):
            for name in (*directories, *filenames):
                info = (Path(parent) / name).lstat()
                blocks += info.st_blocks * 512
                logical += info.st_size
                inodes += 1
        return {"allocated_bytes": blocks, "logical_bytes": logical, "inodes": inodes}
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, _ = _engraving_fixture(project, base_factory=_member_fixture)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            baseline = allocation(project)
            peak = dict(baseline)
            samples = 0
            def measure(phase):
                nonlocal samples
                samples += 1
                current = allocation(project)
                for key in peak:
                    peak[key] = max(peak[key], current[key])
            assert install_fixture_graph(owner, plan, **arguments, interrupt=measure)["graph_accepted"]
            installed = allocation(project)
            with sqlite3.connect(plan) as frozen:
                transform_count, transform_bytes = frozen.execute("SELECT count(*),sum(after_bytes) FROM transformations").fetchone()
                retained_metadata = frozen.execute("SELECT sum(size) FROM files WHERE disposition='Transform'").fetchone()[0]
            controls = project / access.CONTROL
            components = {
                "preparation_registry_existing_bytes": preparation.stat().st_size,
                "installed_registry_bytes": (project / "data/indexes/resources.sqlite3").stat().st_size,
                "graph_journal_bytes": next(controls.glob("graph-*.sqlite3")).stat().st_size,
                "task2_journal_bytes": next(controls.glob("rename-*.sqlite3")).stat().st_size,
                "transformed_metadata_count": transform_count,
                "transformed_metadata_logical_bytes": transform_bytes,
                "retained_metadata_logical_bytes": retained_metadata,
                "new_index_logical_bytes": sum((project / reference).stat().st_size for reference in OUTPUTS),
            }
            assert rollback_fixture_graph(owner, plan, **arguments, interrupt=measure)["task3_reversed"]
            print("TASK3_STORAGE " + json.dumps({"baseline_after_task2": baseline, "installed": installed, "sampled_peak_including_reversal": peak,
                  "peak_growth": {key: peak[key] - baseline[key] for key in peak}, "samples": samples, "components": components}, sort_keys=True))


@pytest.mark.parametrize("action", ["resume", "rollback"])
@pytest.mark.parametrize("boundary", ["index-rebuilding", "index-staged", "index-built:before-sync", "index-replacement", "index-installed:before-sync"])
def test_unsealed_staging_and_sealed_recovery(boundary, action):
    from services import resource_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _full_fixture(project)
        previous = {reference: (project / reference).read_bytes() for reference in OUTPUTS}
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            def stop(phase):
                if phase == boundary:
                    raise RuntimeError("completion recovery interruption")
            with pytest.raises(RuntimeError):
                resource_graph.install_fixture_graph(owner, plan, **arguments, interrupt=stop)
            if action == "resume":
                assert resource_graph.install_fixture_graph(owner, plan, **arguments)["graph_accepted"]
            else:
                assert resource_graph.rollback_fixture_graph(owner, plan, **arguments)["task3_reversed"]
                assert all((project / reference).read_bytes() == value for reference, value in previous.items())


@pytest.mark.parametrize("action", ["resume", "rollback"])
@pytest.mark.parametrize("boundary", ["registry-copy-intent:synced", "registry-snapshot-created", "registry-snapshot:synced", "registry-pair-committed", "registry-prepared:synced", "registry-installed"])
def test_registry_snapshot_recovery_and_reversal(boundary, action):
    from services import resource_graph
    with access.temporary_rename_project() as project:
        plan, checksum, preparation, _ = _full_fixture(project)
        original = preparation.read_bytes()
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            def stop(phase):
                if phase == boundary:
                    raise RuntimeError("snapshot recovery interruption")
            with pytest.raises(RuntimeError):
                resource_graph.install_fixture_graph(owner, plan, **arguments, interrupt=stop)
            if action == "resume":
                assert resource_graph.install_fixture_graph(owner, plan, **arguments)["graph_accepted"]
            else:
                assert resource_graph.rollback_fixture_graph(owner, plan, **arguments)["task3_reversed"]
                assert resource_graph.rollback_fixture_graph(owner, plan, **arguments)["task3_reversed"]
            assert preparation.read_bytes() == original


@pytest.mark.parametrize("boundary", [None, "context-installation", "context-installed"])
def test_durable_canonical_context_defaults_and_restart(boundary):
    from services.resource_graph import install_fixture_graph, rollback_fixture_graph
    from services.resource_resolution import resolve_silhouette
    from data.annotate import get_annotation_json_path
    with access.temporary_rename_project() as project:
        plan, checksum, _, records = _full_fixture(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            if boundary:
                def stop(phase):
                    if phase == boundary:
                        raise RuntimeError("context interruption")
                with pytest.raises(RuntimeError):
                    install_fixture_graph(owner, plan, **arguments, interrupt=stop)
            assert install_fixture_graph(owner, plan, **arguments)["graph_accepted"]
        with pytest.raises(access.MaintenanceClosed):
            resolve_silhouette(project, records[0][1])
        with access.maintenance(project, "graph", approved=True) as owner:
            with access.fixture_graph_access(owner):
                for record, instance, _, media in records:
                    resolved = resolve_silhouette(project, instance)
                    assert resolved["layout"] == "media-first-v1"
                    assert get_annotation_json_path(project, media["filename"], record["media_type"]).is_file()
            assert rollback_fixture_graph(owner, plan, **arguments)["task3_reversed"]


@pytest.mark.parametrize("consumer", ["cli", "mcp", "project", "shotlist", "segmentation", "illustration", "indexing", "engraving"])
def test_representative_canonical_consumers(consumer, monkeypatch, capsys):
    from types import SimpleNamespace
    from services.resource_graph import install_fixture_graph
    from data import resource_locations_legacy as legacy
    from visualizers.window_visualizer import WindowVisualizer
    with access.temporary_rename_project() as project:
        plan, checksum, _, records = _full_fixture(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            install_fixture_graph(owner, plan, **arguments)
            def no_legacy(*args, **kwargs):
                pytest.fail("canonical consumer reached legacy authority")
            monkeypatch.setattr(legacy, "annotation_v0_json_path", no_legacy)
            monkeypatch.setattr(legacy, "registered_silhouette", no_legacy)
            monkeypatch.setattr(legacy, "registered_engraving", no_legacy)
            monkeypatch.setattr("tool.prefs.get", lambda key, default=None, **kwargs: str(project) if key == "path" else default)
            monkeypatch.setattr("tool.prefs.set", lambda *args, **kwargs: None)
            record, instance, _, media = records[0]
            with access.fixture_graph_access(owner):
                if consumer == "cli":
                    import cli
                    cli._annotate_audit(SimpleNamespace(media="movie"))
                    assert "missing" in capsys.readouterr().out.lower()
                elif consumer == "mcp":
                    from mcp_server import mcp_server
                    monkeypatch.setattr(mcp_server, "_ctx", lambda: (str(project), "movie"))
                    assert '"has_annotations": true' in mcp_server.list_movies()
                elif consumer == "project":
                    from services.corpus_stats import get_corpus_stats_state
                    assert get_corpus_stats_state(str(project))["state"] == "ready"
                elif consumer == "shotlist":
                    from visualizers.shot_visualizer import _get_annotation_json_path
                    assert _get_annotation_json_path(project, media["filename"], "movie").is_file()
                elif consumer in ("segmentation", "illustration"):
                    view = SimpleNamespace(_project_path=str(project))
                    registry = WindowVisualizer._resource_registry.fget(view)
                    assert registry == project / "data/indexes/resources.sqlite3"
                    if consumer == "segmentation":
                        from data.resource_locations import resolve_annotation
                        shots = json.loads(resolve_annotation(project, media["filename"], "movie", registry=registry).read_text())
                        assert shots[0]["shot"]["best_frame"]["frame"] == 14
                    else:
                        from visualizers.illustration_visualizer import IllustrationPane
                        annotation, status = IllustrationPane._source_annotation_for_record(view, record)
                        assert annotation == {"objects": ["tree"]} and not status
                elif consumer == "indexing":
                    from data.resource_locations import annotation_files
                    assert len(list(annotation_files(project, "movie"))) == 3
                else:
                    from services.resource_resolution import resolve_source_silhouette
                    result = resolve_source_silhouette(project, instance_id=instance)
                    assert result["record"]["shot_id"] == record["shot_id"]


@pytest.mark.parametrize("defect", ["missing-context", "wrong-project", "missing-receipt", "changed-registry", "legacy-index"])
def test_consumer_context_defects_are_rejected(defect):
    from services.resource_graph import install_fixture_graph
    from services.resource_resolution import resolve_silhouette, resolve_index_record
    with access.temporary_rename_project() as project:
        plan, checksum, _, records = _full_fixture(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            install_fixture_graph(owner, plan, **arguments)
            context = project / access.CONTROL / access.CANONICAL_CONTEXT
            if defect == "missing-context":
                context.unlink()
            elif defect == "wrong-project":
                payload = json.loads(context.read_text())
                payload["inode"] += 1
                context.write_text(json.dumps(payload))
            elif defect == "missing-receipt":
                with sqlite3.connect(next((project / access.CONTROL).glob("graph-*.sqlite3"))) as database:
                    database.execute("DELETE FROM acceptance")
            elif defect == "changed-registry":
                with sqlite3.connect(project / "data/indexes/resources.sqlite3") as database:
                    database.execute("UPDATE shots SET high_water=high_water+1")
            with access.fixture_graph_access(owner):
                with pytest.raises((ValueError, access.MaintenanceError)):
                    if defect == "legacy-index":
                        resolve_index_record(project, {"path": "obsolete.json"})
                    else:
                        resolve_silhouette(project, records[0][1])


@pytest.mark.parametrize("defect", ["wrong-shot", "duplicate-identity", "incorrect-best", "missing-best", "changed-mask", "wrong-engraving-source", "stale-annotation", "index-identity", "missing-resource", "legacy-fallback", "collection-commitment", "wrong-media-type", "partial-transform", "vocabulary-content", "viability-content"])
def test_complete_semantic_graph_rejects_misdirected_edges(defect):
    from services.resource_graph import install_fixture_graph, validate_fixture_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, records = _engraving_fixture(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            install_fixture_graph(owner, plan, **arguments)
            registry = project / "data/indexes/resources.sqlite3"
            with sqlite3.connect(registry) as database:
                reference = database.execute("SELECT record_path FROM resource_locations WHERE kind='silhouettes' ORDER BY instance_id LIMIT 1").fetchone()[0]
                image = database.execute("SELECT image_path FROM resource_locations WHERE kind='silhouettes' ORDER BY instance_id LIMIT 1").fetchone()[0]
                engraving = database.execute("SELECT record_path FROM resource_locations WHERE kind='engravings'").fetchone()[0]
            if defect in ("wrong-shot", "duplicate-identity", "wrong-media-type", "partial-transform"):
                payload = json.loads((project / reference).read_text())
                if defect == "wrong-shot":
                    payload["shot_id"] = records[1][0]["shot_id"]
                elif defect == "duplicate-identity":
                    payload["instance_id"] = records[1][1]
                elif defect == "wrong-media-type":
                    payload["media_type"] = "gameplay"
                else:
                    payload.pop("publication_contract")
                (project / reference).write_text(json.dumps(payload))
            elif defect in ("incorrect-best", "stale-annotation"):
                with access.fixture_graph_access(owner):
                    from data.resource_locations import resolve_annotation
                    path = resolve_annotation(project, records[0][3]["filename"], "movie")
                payload = json.loads(path.read_text())
                if defect == "incorrect-best":
                    payload[0]["shot"]["best_frame"]["frame"] = 12
                else:
                    payload[0]["shot"]["annotation"]["objects"] = ["not the approved tree"]
                path.write_text(json.dumps(payload))
            elif defect == "missing-best":
                with sqlite3.connect(plan) as frozen:
                    path = frozen.execute("SELECT source FROM files WHERE disposition='Retain' AND owner='services.frame_match' ORDER BY source LIMIT 1").fetchone()[0]
                (project / path).unlink()
            elif defect == "changed-mask":
                (project / image).write_bytes(b"not the approved mask bytes")
            elif defect == "missing-resource":
                (project / reference).unlink()
            elif defect == "wrong-engraving-source":
                payload = json.loads((project / engraving).read_text())
                payload["source_instance_id"] = records[1][1]
                (project / engraving).write_text(json.dumps(payload))
            elif defect == "legacy-fallback":
                with sqlite3.connect(registry) as database:
                    database.execute("UPDATE resource_locations SET record_path='data/silhouettes/catalog/movie/obsolete/object_0001.json',revision=revision+1 WHERE record_path=?", (reference,))
            elif defect == "collection-commitment":
                with sqlite3.connect(registry) as database:
                    database.execute("UPDATE annotation_layouts SET revision=revision+1")
            else:
                reference = "data/vocabulary/vocabulary-movie.json" if defect == "vocabulary-content" else "data/indexes/viability/movie-silhouette-viability.sqlite3" if defect == "viability-content" else "data/indexes/illustration/movie-silhouettes.sqlite3"
                if defect == "vocabulary-content":
                    payload = json.loads((project / reference).read_text())
                    payload["fields"] = {"objects": {"invented": {"count": 100, "aliases": []}}}
                    (project / reference).write_text(json.dumps(payload))
                else:
                    with sqlite3.connect(project / reference) as database:
                        if defect == "viability-content":
                            database.execute("UPDATE viability SET metrics='{}'")
                        else:
                            payload = json.loads(database.execute("SELECT payload FROM records LIMIT 1").fetchone()[0])
                            payload["instance_id"] = records[3][1]
                            database.execute("UPDATE records SET payload=?", (json.dumps(payload),))
                with sqlite3.connect(next((project / access.CONTROL).glob("graph-*.sqlite3"))) as evidence:
                    evidence.execute("UPDATE index_outputs SET after_sha=? WHERE source=?", (digest(project / reference), reference))
            result = validate_fixture_graph(owner, plan, **arguments)
            assert not result["graph_accepted"]
            assert result["failure_count"] > 0
            if defect in ("vocabulary-content", "viability-content", "index-identity"):
                assert "invalid_index" in {item["category"] for item in result["failures"]}


def test_storage_budget_uses_unique_directories_and_shared_allocations():
    from services.resource_graph import graph_storage_budget
    with access.temporary_rename_project() as project:
        plan, _, preparation, _ = _member_fixture(project)
        budget = graph_storage_budget(plan, preparation_bytes=preparation.stat().st_size)
        with sqlite3.connect(plan) as database:
            destinations = [row[0] for row in database.execute("SELECT destination FROM files WHERE disposition IN ('Move','Transform')")]
        expected = set()
        for destination in destinations:
            parent = Path(destination).parent
            while parent != Path('.'):
                expected.add(str(parent))
                parent = parent.parent
        assert budget["unique_destination_directories"] == len(expected)
        assert len(expected) < sum(value.count('/') for value in destinations)
        assert budget["components"]["renamed_payload_copies"] == 0
        assert budget["components"]["metadata_before_image_copies"] == 0
        assert budget["peak_additional_bytes"] == sum(budget["components"].values())
        assert budget["components"]["safety_margin"] == 4 * 1024**3
        assert not budget["production_approved"]


def test_storage_budget_credits_exact_completed_metadata_blocks_only():
    from services.resource_graph import graph_storage_budget
    with access.temporary_rename_project() as project:
        plan, _, preparation, _ = _member_fixture(project)
        full = graph_storage_budget(plan, preparation_bytes=preparation.stat().st_size)
        remaining = graph_storage_budget(plan, preparation_bytes=preparation.stat().st_size,
                                         transformation_pending=lambda row: False)
        assert remaining["components"]["transformed_metadata_blocks"] == 0
        assert full["peak_additional_bytes"] - remaining["peak_additional_bytes"] == full["components"]["transformed_metadata_blocks"]
        assert remaining["components"]["safety_margin"] == full["components"]["safety_margin"] == 4 * 1024**3
        assert remaining["components"]["one_working_registry"] == full["components"]["one_working_registry"]


def test_storage_budget_reserves_rollback_move_destination_directories():
    from services.resource_graph import graph_storage_budget
    with access.temporary_rename_project() as project:
        plan, _, preparation, _ = _member_fixture(project)
        with sqlite3.connect(plan) as database:
            rows = database.execute("SELECT source,destination FROM files WHERE disposition='Move'").fetchall()
        old_parents = {str(Path(source).parent) for source, destination in rows}
        missing = {str(Path(destination).parent) for source, destination in rows} - old_parents
        budget = graph_storage_budget(plan, preparation_bytes=preparation.stat().st_size, direction="rollback",
                                      work_pending=lambda row: row["disposition"] == "Move",
                                      directory_pending=lambda reference: reference in missing)
        assert missing
        assert budget["unique_destination_directories"] == len(missing)
        assert budget["components"]["directory_and_entry_blocks"] >= len(missing) * 8192
        assert budget["components"]["safety_margin"] == 4 * 1024**3


@pytest.mark.parametrize("boundary", ["context-reversal-intent:synced", "context-retired", "context-reversed:synced"])
def test_context_reversal_is_durable_and_repeated(boundary):
    from services.resource_graph import install_fixture_graph, rollback_fixture_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _full_fixture(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            install_fixture_graph(owner, plan, **arguments)
            def stop(phase):
                if phase == boundary:
                    raise RuntimeError("context reversal interrupted")
            with pytest.raises(RuntimeError):
                rollback_fixture_graph(owner, plan, **arguments, interrupt=stop)
            assert rollback_fixture_graph(owner, plan, **arguments)["task3_reversed"]
            assert rollback_fixture_graph(owner, plan, **arguments)["task3_reversed"]


@pytest.mark.parametrize("defect", ["changed-final", "changed-unsealed", "corrupt-journal", "missing-journal", "temporary-sidecar", "staging-sidecar", "multiple-temporaries", "repeated-temporary"])
def test_recovery_evidence_and_owner_temporaries(defect):
    from services import resource_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _full_fixture(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            def stop(phase):
                if phase == "index-staged":
                    raise RuntimeError("unsealed output interrupted")
            with pytest.raises(RuntimeError):
                resource_graph.install_fixture_graph(owner, plan, **arguments, interrupt=stop)
            journal = next((project / access.CONTROL).glob("graph-*.sqlite3"))
            with sqlite3.connect(journal) as database:
                reference, staging = database.execute("SELECT source,staging FROM index_outputs WHERE state='building'").fetchone()
            staged = project / staging
            preserved = staged.read_bytes()
            if defect == "changed-final":
                (project / reference).write_bytes(b"independent output")
            elif defect == "changed-unsealed":
                def retire_stop(phase):
                    if phase == "unsealed-retain-intent:synced":
                        raise RuntimeError("retention interrupted")
                with pytest.raises(RuntimeError):
                    resource_graph.install_fixture_graph(owner, plan, **arguments, interrupt=retire_stop)
                staged.write_bytes(b"independently changed staged output")
            elif defect == "corrupt-journal":
                with sqlite3.connect(journal) as database:
                    database.execute("UPDATE meta SET payload='{}'")
            elif defect == "missing-journal":
                journal.unlink()
            elif defect == "staging-sidecar":
                sidecar = Path(str(staged) + "-journal")
                sidecar.write_bytes(b"old SQLite staging journal")
                assert resource_graph.install_fixture_graph(owner, plan, **arguments)["graph_accepted"]
                assert Path(str(sidecar) + ".unsealed").read_bytes() == b"old SQLite staging journal"
                assert Path(str(staged) + ".unsealed").read_bytes() == preserved
                return
            elif defect in ("multiple-temporaries", "repeated-temporary"):
                staged.unlink()
                temporary = staged.parent / ("." + staged.name + ".first.tmp")
                temporary.write_bytes(b"first partial owner output")
                if defect == "repeated-temporary":
                    def retain_stop(phase):
                        if phase == "unsealed-retained:synced":
                            raise RuntimeError("retained temporary interruption")
                    with pytest.raises(RuntimeError):
                        resource_graph.install_fixture_graph(owner, plan, **arguments, interrupt=retain_stop)
                (staged.parent / ("." + staged.name + ".second.tmp")).write_bytes(b"second partial owner output")
            else:
                staged.unlink()
                temporary = staged.parent / ("." + staged.name + ".fixture.tmp")
                temporary.write_bytes(b"incomplete owner temporary")
                sidecar = staged.parent / (temporary.name + "-journal")
                sidecar.write_bytes(b"old SQLite journal")
                assert resource_graph.install_fixture_graph(owner, plan, **arguments)["graph_accepted"]
                assert Path(str(temporary) + ".unsealed").read_bytes() == b"incomplete owner temporary"
                assert Path(str(sidecar) + ".unsealed").read_bytes() == b"old SQLite journal"
                return
            with pytest.raises((ValueError, OSError, sqlite3.Error, access.MaintenanceError)):
                resource_graph.install_fixture_graph(owner, plan, **arguments)
            if defect == "changed-final":
                assert (project / reference).read_bytes() == b"independent output"


@pytest.mark.parametrize("boundary", ["final-acceptance:synced", "context-installation", "context-installed"])
def test_late_acceptance_mutation_never_qualifies(boundary):
    from services import resource_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _full_fixture(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            with sqlite3.connect(plan) as database:
                destination = database.execute("SELECT destination FROM files WHERE disposition='Move' LIMIT 1").fetchone()[0]
            def corrupt(phase):
                if phase == boundary:
                    (project / destination).write_bytes(b"late independent change")
            with pytest.raises(ValueError, match="Graph changed at final acceptance"):
                resource_graph.install_fixture_graph(owner, plan, **arguments, interrupt=corrupt)
            with access.fixture_graph_access(owner):
                with pytest.raises(access.MaintenanceError, match="Unaccepted"):
                    access.canonical_registry(project)


@pytest.mark.parametrize("output", ["registry", "index"])
def test_underlying_file_sync_failure_recovers(output, monkeypatch):
    from services import resource_graph
    with access.temporary_rename_project() as project:
        plan, checksum, _, _ = _full_fixture(project)
        access.enroll(project, approved=True)
        arguments = {"approved_digest": checksum, "plan_identity": "2B.3d", "clients_stopped": True}
        original = resource_graph._file_sync
        def fail(path):
            if (output == "registry" and path.name.startswith(".graph-")) or (output == "index" and path.name.endswith(".new")):
                raise OSError(5, "fixture underlying fsync failure")
            return original(path)
        with access.maintenance(project, "graph", approved=True) as owner:
            execute_fixture_renames(owner, plan, **arguments)
            monkeypatch.setattr(resource_graph, "_file_sync", fail)
            with pytest.raises(OSError, match="fsync failure"):
                resource_graph.install_fixture_graph(owner, plan, **arguments)
            monkeypatch.setattr(resource_graph, "_file_sync", original)
            assert resource_graph.install_fixture_graph(owner, plan, **arguments)["graph_accepted"]