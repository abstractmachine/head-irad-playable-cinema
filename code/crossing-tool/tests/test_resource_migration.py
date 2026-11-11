import copy
import json
import sqlite3

import pytest

from services.resource_migration import temporary_space, transform_manifest, transform_silhouette
from services.resource_migration import activation_ready, correction_proposal, recovery_action, registry_adoption_plan, rehearsal, transform_engraving
from test_resource_storage import proposal


def test_silhouette_transform_preserves_semantics_and_best():
    record = {"shot_id": "tmdb_1@f000010-f000020", "field": "objects", "label": "NOTICES board", "png": "object_0008.png",
              "frame": 12, "source_frame": "media/frames/best/movie/Film/tmdb_1@f000010-f000020.png",
              "assignment": {"state": "active"}, "human_best": True, "source_validation": {"dirty": True}, "unknown": {"evidence": [1]}}
    before = copy.deepcopy(record)
    result = transform_silhouette(record, "tmdb_1@f000010-f000020#0001")
    assert record == before
    assert result["png"] == "silhouette.png"
    assert all(result[key] == before[key] for key in before if key != "png")
    with pytest.raises(ValueError, match="shot"):
        transform_silhouette(record, "tmdb_2@f000010-f000020#0001")


def test_manifest_transform_only_changes_known_locations():
    record = {"version": 1, "json": {"path": "old/film.annotations.json", "hash": "sha256:original", "filename": "film.annotations.json"},
              "npy": {"path": "old/film.frames.npy", "shape": [3, 4]}, "unknown": {"seed": 42}}
    result = transform_manifest(record, {"old/film.annotations.json": "new/film.annotations.json", "old/film.frames.npy": "new/film.frames.npy"})
    assert result["json"]["hash"] == record["json"]["hash"]
    assert result["npy"]["shape"] == [3, 4] and result["unknown"] == record["unknown"]
    with pytest.raises(ValueError, match="Unknown"):
        transform_manifest({"version": 2}, {})
    with pytest.raises(ValueError, match="Unmapped"):
        transform_manifest(record, {})


def test_space_accounts_for_metadata_registry_indexes_without_binary_copy():
    result = temporary_space(metadata_before_bytes=10, metadata_after_bytes=20, registry_bytes=100, replacement_index_bytes=300, journal_bytes=40, margin_bytes=50)
    assert result["additional_bytes"] == 620 and result["binary_corpus_copy_bytes"] == 0
    with pytest.raises(ValueError):
        temporary_space(metadata_before_bytes=-1, metadata_after_bytes=0, registry_bytes=0, replacement_index_bytes=0, journal_bytes=0, margin_bytes=0)


def test_documented_manifest_alias_does_not_rebless_stale_hash():
    record = {"version": 1, "json": {"path": "old/film.json", "filename": "film.json", "hash": "sha256:stale"}}
    result = transform_manifest(record, {"old/film.json": "new/film.annotations.json"}, legacy_aliases={"old/film.json"})
    assert result["json"]["filename"] == "film.annotations.json"
    assert result["json"]["hash"] == "sha256:stale"
    with pytest.raises(ValueError, match="filenames"):
        transform_manifest(record, {"old/film.json": "new/film.annotations.json"})


@pytest.mark.parametrize("film,media_id,shot,frame,label,setting,objects,object_name,expected", [
    ("3 Bad Men (1926) {tmdb-32716}", "tmdb_32716", "f006559-f006723", 6647, "field", "field", [], "object_0001", "setting"),
    ("Big Jake (1971) {tmdb-21717}", "tmdb_21717", "f018624-f018728", 18702, "fields", "camp", ["gun", "potted plants", "birdcages", "door"], "object_0001", None),
    ("Far And Away (1992) {tmdb-011259}", "tmdb_11259", "f021323-f021498", 21373, "fields", "coast", [], "object_0001", None),
    ("Far And Away (1992) {tmdb-011259}", "tmdb_11259", "f021323-f021498", 21373, "fields", "coast", [], "object_0002", None),
    ("Far And Away (1992) {tmdb-011259}", "tmdb_11259", "f021323-f021498", 21373, "fields", "coast", [], "object_0003", None),
    ("Neath The Arizona Skies (1934) {tmdb-53830}", "tmdb_53830", "f001221-f001624", 1361, "field", "field", [], "object_0001", "setting"),
])
def test_six_correction_proposals_require_evidence_and_never_allocate(tmp_path, film, media_id, shot, frame, label, setting, objects, object_name, expected):
    from data import resource_locations as locations
    metadata = tmp_path / "data/metadata/movie.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"media": [{"filename": film + ".mp4", "media_id": media_id}]}))
    shot_id = media_id + "@" + shot
    path = tmp_path / "data/silhouettes/catalog/movie" / film / label / (object_name + ".json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"filename": film + ".mp4", "media_id": media_id, "media_type": "movie", "shot_id": shot_id, "frame": frame, "label": label, "field": None}))
    path.with_suffix(".png").write_bytes(b"unchanged image")
    annotation = locations.annotation_path(tmp_path, film + ".mp4", "movie")
    annotation.parent.mkdir(parents=True)
    annotation.write_text(json.dumps([{"shot": {"shot_id": shot_id, "annotation": {"setting": setting, "objects": objects}}}]))
    before = path.read_bytes(), annotation.read_bytes()
    result = correction_proposal(tmp_path, str(path.relative_to(tmp_path)))
    assert result["proposed_field"] == expected
    assert result["human_approval_required"] and result["instance_id"] is None
    assert result["minimal_patch"] == ({"field": expected} if expected else None)
    assert (path.read_bytes(), annotation.read_bytes()) == before
    assert not (tmp_path / "data/indexes/resources.sqlite3").exists()


def test_registry_plan_preserves_identity_and_adoption_schema(proposal):
    project, source, target, instance, path, record = proposal
    before = source.read_bytes()
    result = registry_adoption_plan(source)
    assert result["identities"] == 1 and result["initial_location_revision"] == 1
    assert "adopt_registry" in result["upgrades"][0]
    assert source.read_bytes() == before and not target.exists()
    with sqlite3.connect(source) as database:
        assert database.execute("SELECT instance_id FROM instances").fetchone()[0] == instance


def test_registry_plan_detects_explicit_collection_journal_upgrade(proposal):
    from data.resource_storage import adopt_registry
    project, source, target, instance, path, record = proposal
    adopt_registry(project, source, target, exceptions=[], approved=True)
    with sqlite3.connect(target) as database:
        database.execute("DROP INDEX one_pending_publication")
        database.execute("DROP TABLE publication_operations")
        database.execute("CREATE TABLE publication_operations(operation_id TEXT PRIMARY KEY,instance_id TEXT NOT NULL,kind TEXT,mode TEXT,state TEXT,details TEXT)")
    result = registry_adoption_plan(target)
    assert result["identities"] == 1
    assert any("upgrade_collection_journal" in upgrade for upgrade in result["upgrades"])


def test_registry_plan_refuses_implicit_old_run_schema_upgrade(proposal):
    from data.resource_storage import adopt_registry
    project, source, target, instance, path, record = proposal
    adopt_registry(project, source, target, exceptions=[], approved=True)
    with sqlite3.connect(target) as database:
        database.execute("ALTER TABLE generation_runs RENAME TO previous_generation_runs")
        database.execute("CREATE TABLE generation_runs(instance_id TEXT,provider TEXT,number INTEGER,mode TEXT,state TEXT CHECK(state IN ('prepared','complete')))")
    before = target.read_bytes()
    result = registry_adoption_plan(target)
    assert any("explicit generation_runs upgrade required" in upgrade for upgrade in result["upgrades"])
    assert target.read_bytes() == before and result["production_adoption_performed"] is False


@pytest.mark.parametrize("state", ["planned", "prepared", "installed", "verified", "failed"])
def test_interruption_recovery_reconciles_hashes_without_overwrite(state):
    assert recovery_action(state, "before", None, "before", "after") == "prepare-or-retry"
    assert recovery_action(state, None, "after", "before", "after") == "verify-before-commit"
    assert recovery_action(state, "before", "changed", "before", "after") == "stop-conflict-no-overwrite"


@pytest.mark.parametrize("change", [{"verified": 1}, {"unresolved": 6}, {"failures": 1}, {"indexes_ready": False}, {"recovery_source_verified": False}, {"writers_quiescent": False}])
def test_no_partial_or_unprotected_activation(change):
    values = {"planned": 2, "verified": 2, "unresolved": 0, "failures": 0, "indexes_ready": True, "recovery_source_verified": True, "writers_quiescent": True}
    assert activation_ready(**values)
    assert not activation_ready(**{**values, **change})


@pytest.mark.parametrize("labels,slug", [(["NOTICES board", "notices board"], "notices-board"), (["stained glass window", "stained-glass window"], "stained-glass-window")])
def test_approved_slugs_preserve_distinct_instance_destinations(labels, slug):
    from data import resource_locations as locations
    from data.media_id import label_slug
    media = {"filename": "Film.mp4", "media_id": "tmdb_1"}
    targets = [locations.silhouette_destination("/fixture", media, "movie", "tmdb_1@f000010-f000020#" + f"{number:04d}", "objects", label)["image"] for number, label in enumerate(labels, 1)]
    assert all(label_slug(label) == slug for label in labels)
    assert targets[0] != targets[1] and all(slug in target.parts for target in targets)


def test_engraving_transform_keeps_generation_review_and_historical_paths():
    from pathlib import Path
    source = {"media_id": "tmdb_1", "media_type": "movie", "filename": "film.mp4", "filename_stem": "film", "shot_id": "tmdb_1@f000010-f000020", "frame": 12, "field": "animals", "label": "bird"}
    record = {"schema_version": "2", "status": "generated", "mode": "frame", "source": {"silhouette_json": "old.json", "silhouette_png": "old.png", "source_frame": "best.png"},
              "generation": {"seed": 42, "prompt": "actual prompt", "parameters": {"steps": 30}}, "review_state": "needs_review", "unknown": {"model": "original"}, "output_png": "old-output.png"}
    before = copy.deepcopy(record)
    result = transform_engraving(record, source, "tmdb_1@f000010-f000020#0001", {"metadata": "new.json", "image": "new.png"}, {"image": Path("engraving-frame.png")}, "runs/local/run-001/run.json")
    assert record == before and result["generation"] == before["generation"]
    assert result["historical_source"] == before["source"]
    assert "silhouette_json" not in result["source"] and result["source_instance_id"].endswith("#0001")
    assert result["review_state"] == before["review_state"] and result["unknown"] == before["unknown"]
    assert isinstance(result["output_png"], str) and json.dumps(result)


def test_rehearsal_requires_separate_approval(tmp_path):
    with pytest.raises(ValueError, match="2B.3d"):
        rehearsal(tmp_path, tmp_path / "absent.sqlite3")


def test_collision_is_review_and_preserves_existing_file(tmp_path):
    from services.resource_migration import _file, _schema
    source = tmp_path / "source.png"
    target = tmp_path / "target.png"
    source.write_bytes(b"original")
    target.write_bytes(b"unrelated")
    with sqlite3.connect(":memory:") as database:
        _schema(database)
        assert _file(database, tmp_path, "source.png", "target.png", "Move", "fixture") is None
        assert database.execute("SELECT disposition FROM files").fetchone()[0] == "Review"
        assert database.execute("SELECT blocking FROM issues").fetchone()[0] == 1
    assert source.read_bytes() == b"original" and target.read_bytes() == b"unrelated"


def test_complete_miniature_preparation_preserves_all_sources(proposal):
    from pathlib import Path
    from data import resource_locations as locations
    from data.resource_storage import digest
    from services.resource_migration import prepare_project
    project, old_proposal, registry, instance, source, record = proposal
    proposal_path = project / "outputs/tests/silhouette-canonical-identity/registry-proposal.sqlite3"
    proposal_path.write_bytes(old_proposal.read_bytes())
    research = project / "outputs/tests/media-first-migration"
    research.mkdir(parents=True)
    annotation = locations.annotation_path(project, "film.mp4", "movie")
    annotation.parent.mkdir(parents=True)
    annotation.write_text(json.dumps([{"shot": {"shot_id": record["shot_id"], "annotation": {"objects": ["field"]}, "best_frame": {"frame": 12, "score": 0.9}}}]))
    manifest = annotation.with_name("film.annotations.manifest.json")
    manifest.write_text(json.dumps({"version": 1, "json": {"path": "data/annotations/shots/movie/film.json", "filename": "film.json", "hash": "sha256:" + digest(annotation)}}))
    frame = project / "media/frames/best/movie/film/frame.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"original best pixels")
    record["source_frame"] = str(frame.relative_to(project))
    source.write_text(json.dumps(record))
    for media_type in ("movie", "gameplay"):
        index = project / "data/indexes/illustration" / (media_type + "-silhouettes.sqlite3")
        index.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(index) as database:
            database.execute("CREATE TABLE records(payload TEXT,assignment_state TEXT)")
            if media_type == "movie":
                database.execute("INSERT INTO records VALUES(?,'active')", (json.dumps({**record, "path": str(source)}),))
    with sqlite3.connect(proposal_path) as database:
        key = database.execute("SELECT registration_key FROM instances").fetchone()[0]
    media = {"filename": "film.mp4", "media_id": "tmdb_1"}
    target = locations.silhouette_destination(project, media, "movie", instance, "objects", "field")["dir"]
    with sqlite3.connect(research / "migration-plan.sqlite3") as database:
        database.executescript("""
        CREATE TABLE media(media_type,media_id,filename_stem,media_item);
        CREATE TABLE silhouettes(legacy_json,legacy_png,registration_key,instance_id,media_type,media_id,shot_id,media_item,field,label,slug,destination,status);
        CREATE TABLE annotations(source,destination,media_type,media_item,filename_stem,kind,size,mtime_ns,status);
        CREATE TABLE engravings(source,destination,kind,instance_id,reference,status);
        CREATE TABLE unmoved_silhouette_artifacts(path,kind,size,mtime_ns,disposition);
        CREATE TABLE references_found(source,pointer,target,classification,occurrences);
        """)
        database.execute("INSERT INTO media VALUES('movie','tmdb_1','film','film')")
        database.execute("INSERT INTO silhouettes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (str(source.relative_to(project)), str(source.with_suffix(".png").relative_to(project)), key, instance,
                         "movie", "tmdb_1", record["shot_id"], "film", "objects", "field", "field", str(target.relative_to(project)), "planned"))
        for path, suffix in ((annotation, ".annotations.json"), (manifest, ".annotations.manifest.json")):
            destination = locations.annotation_destination(project, media, "movie", suffix)
            database.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?,?)", (str(path.relative_to(project)), str(destination.relative_to(project)), "movie", "film", "film", "shot artifact", path.stat().st_size, path.stat().st_mtime_ns, "planned"))
    with sqlite3.connect(research / "safety-final.sqlite3") as database:
        database.execute("CREATE TABLE files(path TEXT,kind TEXT,size INTEGER,mtime_ns INTEGER,sha256 TEXT)")
        for path in (project / "data").rglob("*"):
            if path.is_file():
                database.execute("INSERT INTO files VALUES(?,'file',?,?,?)", (str(path.relative_to(project)), path.stat().st_size, path.stat().st_mtime_ns, digest(path)))
    before = {str(path): path.read_bytes() for path in (project / "data").rglob("*") if path.is_file()}
    output = project / "outputs/maintenance/fixture-preparation"
    result = prepare_project(project, output)
    with sqlite3.connect(result["plan"]) as database:
        assert database.execute("SELECT count(*) FROM silhouettes").fetchone()[0] == 1
        assert database.execute("SELECT count(*) FROM files").fetchone()[0] == 7
        assert database.execute("SELECT count(*) FROM issues WHERE blocking=1").fetchone()[0] == 0
        assert database.execute("SELECT instance_id FROM identity_bindings").fetchone()[0] == instance
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not database.execute("PRAGMA foreign_key_check").fetchall()
    assert {str(path): path.read_bytes() for path in (project / "data").rglob("*") if path.is_file()} == before
    assert not registry.exists() and not target.exists()
    assert all((output / filename).is_file() for filename in ("correction-proposals.md", "registry-adoption.md", "reference-transformations.md", "execution-and-recovery.md", "rehearsal.md", "readiness.md", "verification.md"))
    checked = rehearsal(project, output / "migration-plan.sqlite3", approved=True)
    assert checked["files_checked"] == 7 and checked["coverage"]["identities"] == 1
    assert checked["coverage"]["collections"] == 1 and checked["coverage"]["index_rows"] == 1
    assert checked["coverage"]["references"] == 3 and checked["coverage"]["retained_dependencies"] == 1
    assert not checked["accepted"] and checked["failure_count"] == 0
    with pytest.raises(ValueError, match="preserved"):
        prepare_project(project, output)
    from services.resource_migration import resume_preparation
    with pytest.raises(ValueError, match="already exists"):
        resume_preparation(project, output)


def test_physical_fixture_interruption_and_rollback_preserve_hashes(tmp_path):
    from data.resource_storage import digest
    source = tmp_path / "source.png"
    target = tmp_path / "target.png"
    source.write_bytes(b"unchanged binary")
    before = digest(source)
    assert recovery_action("prepared", digest(source), None, before, before) == "prepare-or-retry"
    source.rename(target)
    assert recovery_action("prepared", None, digest(target), before, before) == "verify-before-commit"
    target.rename(source)
    assert digest(source) == before and not target.exists()


def test_existing_index_owner_stages_without_replacing_production(tmp_path, monkeypatch):
    from services import illustration_index
    production = illustration_index.index_path(tmp_path, "silhouettes", "movie")
    production.parent.mkdir(parents=True)
    production.write_bytes(b"old production index")
    monkeypatch.setattr(illustration_index, "_scan_silhouettes", lambda project, media: [])
    destination = tmp_path / "outputs/maintenance/fixture/movie-silhouettes.sqlite3"
    result = illustration_index._rebuild_index(tmp_path, "silhouettes", "movie", {}, {}, destination=destination)
    assert result["status"] == "ready" and result["count"] == 0
    assert production.read_bytes() == b"old production index"
    with sqlite3.connect(destination) as database:
        assert database.execute("SELECT value FROM meta WHERE key='source'").fetchone()[0] == "silhouettes"
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(ValueError, match="already exists"):
        illustration_index._rebuild_index(tmp_path, "silhouettes", "movie", {}, {}, destination=destination)


def test_unpublished_index_rejects_wrong_output_root(tmp_path, monkeypatch):
    from services import illustration_index
    monkeypatch.setattr(illustration_index, "_scan_silhouettes", lambda project, media: [])
    with pytest.raises(ValueError, match="outputs/maintenance"):
        illustration_index._rebuild_index(tmp_path, "silhouettes", "movie", {}, {}, destination=tmp_path / "data/indexes/replacement.sqlite3")


def test_symbolic_frame_reference_is_provenance_not_missing_file(tmp_path):
    from services.resource_migration import source_frame_reference_plan
    record = {"shot_id": "tmdb_1@f000010-f000020", "frame": 12, "source_frame": "frame:12"}
    result = source_frame_reference_plan(tmp_path, record)
    assert result["classification"] == "numeric source-frame marker, not a file"
    assert not result["approval_required"] and result["patch"] == {}
    assert source_frame_reference_plan(tmp_path, {**record, "source_frame": "frame:11"})["classification"] == "missing or contradictory reference"


def test_exact_legacy_frame_root_proposal_preserves_historical_path(tmp_path):
    from services.frame_match import best_frame_path
    from services.resource_migration import source_frame_reference_plan
    record = {"media_type": "movie", "filename": "film.mp4", "shot_id": "tmdb_1@f000010-f000020", "frame": 12,
              "source_frame": "/previous/project/media/frames/best/movie/film/tmdb_1@f000010-f000020.png"}
    path = best_frame_path(str(tmp_path), "movie", "film.mp4", record["shot_id"])
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing BEST image")
    before = copy.deepcopy(record)
    result = source_frame_reference_plan(tmp_path, record)
    assert result["approval_required"] and result["patch"]["historical_source_frame"] == before["source_frame"]
    assert result["proposed_target"] == str(path.relative_to(tmp_path))
    assert record == before and path.read_bytes() == b"existing BEST image"
    assert source_frame_reference_plan(tmp_path, {**record, "source_frame": "/previous/other.png"})["proposed_target"] is None


def test_unknown_active_schemas_are_review_not_blind_rewrites():
    with pytest.raises(ValueError, match="Unknown active silhouette"):
        transform_silhouette({"schema_version": "99"}, "tmdb_1@f000010-f000020#0001")
    with pytest.raises(ValueError, match="index type"):
        transform_manifest({"version": 1, "index_type": "unknown-engine"}, {})


def test_retained_checksum_reuse_requires_exact_stat_and_preserves_source(tmp_path):
    from data.resource_storage import digest
    from services.resource_migration import _file, _reuse_retained_checksums, _schema
    source = tmp_path / "source.png"
    other = tmp_path / "other.png"
    source.write_bytes(b"retained image")
    other.write_bytes(b"changed source")
    evidence = tmp_path / "evidence.sqlite3"
    with sqlite3.connect(evidence) as database:
        database.execute("CREATE TABLE protected(path TEXT PRIMARY KEY,size INTEGER,mtime_ns INTEGER,sha256 TEXT)")
        database.execute("INSERT INTO protected VALUES(?,?,?,?)", ("source.png", source.stat().st_size, source.stat().st_mtime_ns, digest(source)))
        database.execute("INSERT INTO protected VALUES(?,?,?,?)", ("other.png", other.stat().st_size + 1, other.stat().st_mtime_ns, digest(other)))
    before = source.read_bytes(), other.read_bytes(), evidence.read_bytes()
    with sqlite3.connect(":memory:", uri=True) as database:
        _schema(database)
        _file(database, tmp_path, "source.png", None, "Retain", "fixture")
        _file(database, tmp_path, "other.png", None, "Retain", "fixture")
        result = _reuse_retained_checksums(database, evidence)
        assert result["matched_sources"] == 1 and result["unfrozen_sources"] == 1
        assert not result["fresh_corpus_hashing_performed"]
        assert database.execute("SELECT sha256 FROM files WHERE source='source.png'").fetchone()[0] == digest(source)
        assert database.execute("SELECT sha256 FROM files WHERE source='other.png'").fetchone()[0] is None
    assert (source.read_bytes(), other.read_bytes(), evidence.read_bytes()) == before