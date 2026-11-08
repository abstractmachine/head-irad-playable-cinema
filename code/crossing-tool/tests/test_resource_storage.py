import json
import sqlite3

import pytest

from services.engraving_paths import read_engraving_meta, repair_engraving_meta
from data.resource_storage import ResourceStore, adopt_registry
from services.silhouette_canonical_identity import InstanceRegistry, RESEARCH, prepare_registration
from data import resource_locations as locations
from data.resource_storage import digest
from services.resource_publication import publish_resource, publish_generation_run, recover_publication, publication_status
from services.resource_validation import validate_metadata
from services.resource_resolution import resolve_silhouette, resolve_engraving
from services.resource_corrections import propose_missing_field, apply_missing_field, correction_status


@pytest.fixture
def proposal(tmp_path):
    record = {"media_type": "movie", "media_id": "tmdb_1", "filename": "film.mp4", "filename_stem": "film",
              "shot_id": "tmdb_1@f000010-f000020", "frame": 12, "field": "objects", "label": "field"}
    source = tmp_path / "data/silhouettes/catalog/movie/film/field/object_0001.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(record))
    source.with_suffix(".png").write_bytes(b"legacy fixture")
    registry = InstanceRegistry(tmp_path, tmp_path / RESEARCH / "proposal.sqlite3")
    instance = registry.register([prepare_registration({**record, "path": source}, tmp_path)])[0][1]
    path = registry.path
    registry.close()
    metadata = tmp_path / "data/metadata/movie.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"media": [{"filename": "film.mp4", "media_id": "tmdb_1"}]}))
    return tmp_path, path, tmp_path / "data/indexes/resources.sqlite3", instance, source, record


def test_explicit_adoption_and_idempotency(proposal):
    project, source, target, instance, path, metadata = proposal
    before = source.read_bytes()
    with pytest.raises(ValueError, match="approval"):
        adopt_registry(project, source, target, exceptions=[])
    result = adopt_registry(project, source, target, exceptions=[], approved=True)
    assert result["adopted"] and result["instances"] == 1
    assert not adopt_registry(project, source, target, exceptions=[], approved=True)["adopted"]
    assert source.read_bytes() == before
    with sqlite3.connect(target) as database:
        assert database.execute("SELECT instance_id FROM instances").fetchone()[0] == instance
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("UPDATE instances SET instance_id='replacement'")


@pytest.mark.parametrize("stage", ["validated", "copied"])
def test_interrupted_adoption_retries(proposal, stage):
    project, source, target, instance, path, metadata = proposal
    def interrupt(current):
        if current == stage:
            raise RuntimeError("interrupted")
    with pytest.raises(RuntimeError):
        adopt_registry(project, source, target, exceptions=[], approved=True, interrupt=interrupt)
    assert not target.exists()
    assert adopt_registry(project, source, target, exceptions=[], approved=True)["adopted"]


def test_missing_classification_blocks_adoption(proposal):
    project, source, target, instance, path, metadata = proposal
    metadata.pop("field")
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        adopt_registry(project, source, target, exceptions=[], approved=True)
    assert not target.exists()


def test_explicit_unresolved_exceptions(proposal):
    project, source, target, instance, path, metadata = proposal
    exceptions = [{"reference": "data/silhouettes/catalog/movie/other/bird/object_0001.json", "reason": "missing field"}]
    exception_path = project / exceptions[0]["reference"]
    exception_path.parent.mkdir(parents=True)
    exception_path.write_text(json.dumps({"label": "bird", "media_type": "movie", "filename": "film.mp4", "shot_id": metadata["shot_id"]}))
    adopt_registry(project, source, target, exceptions=exceptions, approved=True)
    with sqlite3.connect(target) as database:
        assert database.execute("SELECT count(*) FROM adoption_exceptions").fetchone()[0] == 1
    with pytest.raises(ValueError, match="Conflicting"):
        adopt_registry(project, source, target, exceptions=[], approved=True)


@pytest.fixture
def writable(proposal):
    from PIL import Image
    project, source, registry, instance, path, metadata = proposal
    adopt_registry(project, source, registry, exceptions=[], approved=True)
    annotation = locations.annotation_path(project, "film.mp4", "movie")
    annotation.parent.mkdir(parents=True)
    annotation.write_text(json.dumps([{"shot": {"shot_id": metadata["shot_id"], "annotation": {"objects": ["field"]}}}]))
    image = project / "input.png"
    Image.new("RGBA", (2, 2), (20, 30, 40, 255)).save(image)
    content = {**metadata, "publication_contract": "media-first-v1", "resource_kind": "silhouettes", "instance_id": instance,
               "bbox": [0, 0, 2, 2], "frame_size": [2, 2], "mask_area": 4, "sam_model": "fixture-sam",
               "frame_match_model": "fixture-clip", "source_frame": "frame:12"}
    return project, registry, instance, content, image


def pending_id(registry):
    with sqlite3.connect(registry) as database:
        return database.execute("SELECT operation_id FROM publication_operations WHERE state NOT IN ('finished','rolled_back')").fetchone()[0]


def test_valid_silhouette_publication(writable):
    project, registry, instance, content, image = writable
    assert publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)["revision"] == 1
    result = resolve_silhouette(project, instance, registry=registry)
    assert result["record"]["field"] == "objects" and result["record"]["label"] == "field"


@pytest.mark.parametrize("stage", ["prepared", "image-installed", "metadata-installed", "before-commit", "committed"])
def test_interrupted_publication_recovery(writable, stage):
    project, registry, instance, content, image = writable
    def interrupt(current):
        if current == stage:
            raise RuntimeError("interrupted")
    with pytest.raises(RuntimeError):
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True, interrupt=interrupt)
    operation = pending_id(registry)
    if stage in ("image-installed", "metadata-installed", "before-commit"):
        with pytest.raises(ValueError, match="recovery"):
            resolve_silhouette(project, instance, registry=registry)
    assert recover_publication(project, registry, operation, approved=True)["state"] == "finished"
    assert recover_publication(project, registry, operation, approved=True)["state"] == "finished"
    assert resolve_silhouette(project, instance, registry=registry)["record"]["instance_id"] == instance


def test_failed_replacement_retains_previous_pair(writable):
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    old = resolve_silhouette(project, instance, registry=registry)
    before = old["metadata"].read_bytes(), old["image"].read_bytes()
    def interrupt(stage):
        if stage == "image-installed":
            raise RuntimeError("interrupted replacement")
    with pytest.raises(RuntimeError):
        publish_resource(project, registry, instance, "silhouettes", {**content, "confidence": 0.9}, image,
                         approved=True, expected_revision=1, interrupt=interrupt)
    recover_publication(project, registry, pending_id(registry), approved=True, action="rollback")
    current = resolve_silhouette(project, instance, registry=registry)
    assert before == (current["metadata"].read_bytes(), current["image"].read_bytes())


@pytest.mark.parametrize("missing", ["field", "label", "bbox", "mask_area", "sam_model"])
def test_incomplete_metadata_cannot_publish(writable, missing):
    project, registry, instance, content, image = writable
    content.pop(missing)
    binding = locations.registered_instance(project, registry, instance)
    assert validate_metadata(project, binding, content, "silhouettes")["state"] == "incomplete"
    with pytest.raises(ValueError, match="rejected"):
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)


def test_both_mode_publications_and_runs(writable):
    project, registry, instance, silhouette, image = writable
    publish_resource(project, registry, instance, "silhouettes", silhouette, image, approved=True)
    source = resolve_silhouette(project, instance, registry=registry)
    media = locations.find_media(project, "movie", media_id="tmdb_1")
    for number, mode in enumerate(("frame", "isolated"), 1):
        generation = {"service": "qwen-local", "model": "fixture-model", "model_version": "v1", "prompt": "fixture prompt", "parameters": {}, "seed": 42,
                  "software": {"crossing": "fixture-v1", "pipeline": "fixture-v1"}}
        run = locations.generation_run_destination(project, media, "movie", instance, "objects", "field", "local", number) / "run.json"
        source_info = {"image_sha256": digest(source["image"]), "mask_association": "silhouette-alpha",
                   "evidence": [{"media_id": "tmdb_1", "frame": 12, "image_sha256": digest(image)}]}
        publish_generation_run(project, registry, instance, "objects", "field", "local", number,
                       {"source_instance_id": instance, "mode": mode, "generation": generation, "source": source_info},
                       {"input.png": image}, approved=True)
        metadata = {**silhouette, "resource_kind": "engravings", "source_instance_id": instance, "mode": mode,
                    "status": "generated", "generation": generation,
                    "source": source_info,
                    "published_run": {"provider": "local", "number": number, "sha256": digest(run)}}
        publish_resource(project, registry, instance, "engravings", metadata, image, mode=mode, approved=True)
    assert resolve_engraving(project, instance, "frame", registry=registry)["metadata"].name == "engraving-frame.json"
    assert resolve_engraving(project, instance, "isolated", registry=registry)["metadata"].name == "engraving-isolated.json"


def test_annotation_enumeration_and_commit(writable):
    project, registry, instance, content, image = writable
    record = locations.find_media(project, "movie", media_id="tmdb_1")
    collection = locations.annotation_artifacts(project, record, "movie", registry=registry)
    assert collection["layout"] == "legacy-v1" and len(collection["expected"]) == 8
    assert not collection["missing_required"] and ".log" in collection["optional"]
    target = locations.annotation_destination(project, record, "movie")
    target.parent.mkdir(parents=True)
    target.write_bytes(collection["existing"][".annotations.json"].read_bytes())
    assert locations.resolve_annotation(project, "film.mp4", "movie", registry=registry) != target
    store = ResourceStore(project, registry, approved=True)
    assert store.commit_annotation_layout(record, "movie", "media-first-v1")["revision"] == 1
    assert locations.resolve_annotation(project, "film.mp4", "movie", registry=registry) == target
    with pytest.raises(ValueError):
        store.commit_annotation_layout(record, "movie", "legacy-v1", expected_revision=0)


@pytest.mark.parametrize("stage", [None, "prepared", "metadata-written"])
def test_missing_field_correction_contract(writable, stage):
    project, registry, instance, content, image = writable
    reference = "data/silhouettes/catalog/movie/film/field/object_0009.json"
    path = project / reference
    historical = {key: content[key] for key in ("media_id", "media_type", "shot_id", "filename", "label")}
    path.write_text(json.dumps(historical))
    with sqlite3.connect(registry) as database:
        database.execute("INSERT INTO adoption_exceptions VALUES(?,?,?)", (reference, "missing field", json.dumps({"reference": reference})))
    before = path.read_bytes()
    proposal = propose_missing_field(project, reference, "objects", "field")
    assert path.read_bytes() == before and "field" not in historical
    with pytest.raises(ValueError):
        apply_missing_field(project, registry, proposal, approval="unapproved", approved=True)
    def interrupt(current):
        if current == stage:
            raise RuntimeError("interrupted")
    if stage:
        with pytest.raises(RuntimeError):
            apply_missing_field(project, registry, proposal, approval=proposal["approval_token"], approved=True, interrupt=interrupt)
        assert correction_status(project, registry, reference)[0]["state"] == "prepared"
    assert apply_missing_field(project, registry, proposal, approval=proposal["approval_token"], approved=True)["state"] == "applied"
    assert json.loads(path.read_text()) == {**historical, "field": "objects"}
    assert not apply_missing_field(project, registry, proposal, approval=proposal["approval_token"], approved=True)["changed"]


@pytest.mark.parametrize("change", ["identity", "field", "frame", "bbox", "media", "mode"])
def test_contradictory_metadata_rejected(writable, change):
    project, registry, instance, content, image = writable
    changes = {"identity": {"instance_id": instance.replace("#0001", "#0002")}, "field": {"field": "settings"},
               "frame": {"frame": 999}, "bbox": {"bbox": [0, 0, 5, 5]}, "media": {"media_id": "tmdb_2"},
               "mode": {"resource_kind": "engravings"}}
    content.update(changes[change])
    with pytest.raises(ValueError, match="rejected"):
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    assert publication_status(project, registry) == []


def test_dirty_and_historical_validation(writable):
    project, registry, instance, content, image = writable
    binding = locations.registered_instance(project, registry, instance)
    historical = {key: value for key, value in content.items() if key not in ("publication_contract", "instance_id")}
    assert validate_metadata(project, binding, historical, "silhouettes")["state"] == "historical"
    dirty = locations.source_validation(instance, content["shot_id"], registered_frame=12, current_frame=13)
    content["source_validation"] = dirty
    assert validate_metadata(project, binding, content, "silhouettes")["dirty"]
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    assert resolve_silhouette(project, instance, registry=registry)["record"]["source_validation"] == dirty


def test_reader_detects_committed_pair_tampering(writable):
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    result = resolve_silhouette(project, instance, registry=registry)
    result["image"].write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        resolve_silhouette(project, instance, registry=registry)


def test_concurrent_publishers_require_revision(writable):
    from concurrent.futures import ThreadPoolExecutor
    project, registry, instance, content, image = writable
    def attempt():
        try:
            return publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)["revision"]
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt), executor.submit(attempt)]
        results = [future.result(timeout=10) for future in futures]
    assert results.count(1) == 1 and any("revision changed" in str(result) for result in results)
    assert resolve_silhouette(project, instance, registry=registry)["revision"] == 1


def test_commit_failure_is_explicitly_recoverable(writable, monkeypatch):
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    original = resolve_silhouette(project, instance, registry=registry)
    before = original["metadata"].read_bytes(), original["image"].read_bytes()
    def fail(*args, **kwargs):
        raise RuntimeError("commit failure")
    monkeypatch.setattr(ResourceStore, "commit_location", fail)
    with pytest.raises(RuntimeError, match="commit failure"):
        publish_resource(project, registry, instance, "silhouettes", {**content, "confidence": 0.75}, image, approved=True, expected_revision=1)
    status = publication_status(project, registry, instance_id=instance)
    pending = next(row for row in status if row["state"] == "files_published")
    recover_publication(project, registry, pending["operation_id"], approved=True, action="rollback")
    restored = resolve_silhouette(project, instance, registry=registry)
    assert before == (restored["metadata"].read_bytes(), restored["image"].read_bytes())


def test_wrong_png_and_missing_pair_rejected(writable):
    project, registry, instance, content, image = writable
    image.write_bytes(b"not a png")
    with pytest.raises(OSError):
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    assert publication_status(project, registry) == []
    image.unlink()
    with pytest.raises(FileNotFoundError):
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)


def test_foreign_destination_is_not_overwritten(writable):
    project, registry, instance, content, image = writable
    record = locations.find_media(project, "movie", media_id="tmdb_1")
    target = locations.silhouette_destination(project, record, "movie", instance, "objects", "field")["image"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"foreign")
    with pytest.raises(ValueError, match="destination"):
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    assert target.read_bytes() == b"foreign"


def test_unsafe_adoption_destination(proposal, tmp_path):
    project, source, target, instance, path, metadata = proposal
    for destination in (project / "data/indexes/../elsewhere.sqlite3", project / "outputs/registry.sqlite3"):
        with pytest.raises(ValueError):
            adopt_registry(project, source, destination, exceptions=[], approved=True)
    target.parent.symlink_to(project / "outputs", target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        adopt_registry(project, source, target, exceptions=[], approved=True)


def test_stale_policy_blocks_adoption(proposal):
    project, source, target, instance, path, metadata = proposal
    with sqlite3.connect(source) as database:
        database.execute("UPDATE meta SET value='unknown-policy' WHERE key='policy'")
    with pytest.raises(ValueError, match="schema"):
        adopt_registry(project, source, target, exceptions=[], approved=True)
    assert not target.exists()


def test_annotation_commitment_detects_changed_or_uncommitted_artifacts(writable):
    project, registry, instance, content, image = writable
    record = locations.find_media(project, "movie", media_id="tmdb_1")
    store = ResourceStore(project, registry, approved=True)
    store.commit_annotation_layout(record, "movie", "legacy-v1")
    path = locations.resolve_annotation(project, "film.mp4", "movie", registry=registry)
    before = path.read_bytes()
    path.write_bytes(before + b" ")
    with pytest.raises(ValueError, match="checksum"):
        locations.resolve_annotation(project, "film.mp4", "movie", registry=registry)
    assert locations.annotation_artifacts(project, record, "movie", registry=registry)["changed"] == [".annotations.json"]
    log = locations.annotation_path(project, "film.mp4", "movie", ".log")
    log.write_text("uncommitted log")
    with pytest.raises(ValueError, match="not been committed"):
        locations.resolve_annotation(project, "film.mp4", "movie", ".log", registry=registry)


@pytest.mark.parametrize("media_type", ["movie", "gameplay"])
def test_complete_annotation_collection_preserves_every_suffix(tmp_path, media_type):
    media_id = "tmdb_7" if media_type == "movie" else "game_fixture_123"
    filename = "Example {tmdb-0007}.mp4" if media_type == "movie" else "descriptive gameplay.mp4"
    record = {"media_id": media_id, "filename": filename}
    metadata = tmp_path / "data/metadata" / (media_type + ".json")
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"media": [record]}))
    collection = locations.annotation_artifacts(tmp_path, record, media_type, layout="media-first-v1")
    assert collection["missing_required"] == [".annotations.json"]
    payloads = {}
    for suffix, path in collection["expected"].items():
        path.parent.mkdir(parents=True, exist_ok=True)
        payloads[suffix] = ("unchanged-" + suffix).encode()
        path.write_bytes(payloads[suffix])
    result = locations.annotation_artifacts(tmp_path, record, media_type, layout="media-first-v1", required=(".annotations.json", ".frames.npy"))
    assert not result["missing_required"] and len(result["existing"]) == 8
    assert {suffix: path.read_bytes() for suffix, path in result["existing"].items()} == payloads
    assert all(path.parent.name == ("Example {tmdb-0007}" if media_type == "movie" else media_id) for path in result["existing"].values())


def test_partial_or_conflicting_annotation_collections_cannot_commit(writable):
    project, registry, instance, content, image = writable
    record = locations.find_media(project, "movie", media_id="tmdb_1")
    legacy = locations.annotation_artifacts(project, record, "movie")
    target = locations.annotation_destination(project, record, "movie")
    target.parent.mkdir(parents=True)
    target.write_bytes(legacy["existing"][".annotations.json"].read_bytes())
    array = legacy["expected"][".frames.npy"]
    array.write_bytes(b"unchanged array")
    store = ResourceStore(project, registry, approved=True)
    with pytest.raises(ValueError, match="Incomplete"):
        store.commit_annotation_layout(record, "movie", "media-first-v1")
    target_array = locations.annotation_destination(project, record, "movie", ".frames.npy")
    target_array.write_bytes(b"different array")
    with pytest.raises(ValueError, match="Conflicting"):
        store.commit_annotation_layout(record, "movie", "media-first-v1")
    assert array.read_bytes() == b"unchanged array"


def test_dirty_cannot_be_cleared_by_incidental_republication(writable):
    project, registry, instance, content, image = writable
    content["source_validation"] = locations.source_validation(instance, content["shot_id"], registered_frame=12, current_frame=13)
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    content.pop("source_validation")
    with pytest.raises(ValueError, match="clear"):
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True, expected_revision=1)


def _engraving_draft(writable, provider="openai", mode="frame"):
    project, registry, instance, silhouette, image = writable
    publish_resource(project, registry, instance, "silhouettes", silhouette, image, approved=True)
    source = resolve_silhouette(project, instance, registry=registry)
    record = locations.find_media(project, "movie", media_id="tmdb_1")
    generation = {"service": provider, "model": "fixture-model", "prompt": "fixture prompt", "parameters": {},
                  "software": {"crossing": "fixture-v1", "backend": "fixture-v1"}}
    if provider == "openai":
        generation.update(seed_policy="provider-managed", version_policy="provider-managed")
    else:
        generation.update(seed=42, model_version="v1")
    run = locations.generation_run_destination(project, record, "movie", instance, "objects", "field", provider, 1) / "run.json"
    source_info = {"image_sha256": digest(source["image"]), "mask_association": "silhouette-alpha",
                   "evidence": [{"media_id": "tmdb_1", "frame": 12, "image_sha256": digest(image)}]}
    publish_generation_run(project, registry, instance, "objects", "field", provider, 1,
                           {"source_instance_id": instance, "mode": mode, "generation": generation, "source": source_info},
                           {"input.png": image}, approved=True)
    content = {**silhouette, "resource_kind": "engravings", "source_instance_id": instance, "mode": mode, "status": "generated",
               "source": source_info,
               "generation": generation, "published_run": {"provider": provider, "number": 1, "sha256": digest(run)}}
    return content


def test_openai_explicit_provider_managed_applicability(writable):
    project, registry, instance, content, image = writable
    content = _engraving_draft(writable)
    assert "seed" not in content["generation"]
    publish_resource(project, registry, instance, "engravings", content, image, mode="frame", approved=True)
    assert resolve_engraving(project, instance, registry=registry)["record"]["generation"]["seed_policy"] == "provider-managed"


@pytest.mark.parametrize("fault", ["mode", "source", "seed_policy", "prompt", "parameters", "software", "run", "evidence", "obsolete_path"])
def test_reproducibility_or_source_mismatch_cannot_publish(writable, fault):
    project, registry, instance, content, image = writable
    content = _engraving_draft(writable)
    if fault == "mode":
        content["mode"] = "isolated"
    elif fault == "source":
        content["source"]["image_sha256"] = "0" * 64
    elif fault in ("seed_policy", "prompt", "parameters", "software"):
        content["generation"].pop(fault)
    elif fault == "run":
        content["published_run"]["sha256"] = "0" * 64
    elif fault == "evidence":
        content["source"]["evidence"] = []
    else:
        content["source"]["silhouette_json"] = "/obsolete/source.json"
    with pytest.raises(ValueError, match="rejected"):
        publish_resource(project, registry, instance, "engravings", content, image, mode="frame", approved=True)
    with pytest.raises(FileNotFoundError):
        resolve_engraving(project, instance, registry=registry)


def test_publication_does_not_resurrect_inactive_source(writable):
    project, registry, instance, content, image = writable
    binding = locations.registered_instance(project, registry, instance)
    path = project / binding["legacy_json"]
    record = json.loads(path.read_text())
    record["assignment"] = {"state": "inactive"}
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="active"):
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    assert publication_status(project, registry) == []


def test_reclassification_keeps_identity_and_authoritative_location(writable):
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    original = resolve_silhouette(project, instance, registry=registry)
    annotation = locations.annotation_path(project, "film.mp4", "movie")
    entries = json.loads(annotation.read_text())
    entries[0]["shot"]["annotation"]["places"] = ["field"]
    annotation.write_text(json.dumps(entries))
    publish_resource(project, registry, instance, "silhouettes", {**content, "field": "places"}, image, approved=True, expected_revision=1)
    current = resolve_silhouette(project, instance, registry=registry)
    assert current["instance_id"] == instance and current["revision"] == 2
    assert current["metadata"] != original["metadata"] and original["metadata"].exists()
    assert current["record"]["field"] == "places"


def test_coherent_read_snapshot_survives_later_replacement(writable):
    from PIL import Image
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    snapshot = resolve_silhouette(project, instance, registry=registry)
    original = snapshot["image_bytes"]
    Image.new("RGBA", (2, 2), (80, 70, 60, 255)).save(image)
    publish_resource(project, registry, instance, "silhouettes", {**content, "confidence": 0.8}, image, approved=True, expected_revision=1)
    assert snapshot["image_bytes"] == original
    assert resolve_silhouette(project, instance, registry=registry)["image_bytes"] != original


@pytest.mark.parametrize("invalid", ["mode", "revision"])
def test_invalid_publication_controls_fail_before_install(writable, invalid):
    project, registry, instance, content, image = writable
    controls = {"mode": "frame"} if invalid == "mode" else {"expected_revision": False}
    with pytest.raises(ValueError):
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True, **controls)
    assert publication_status(project, registry) == []


def test_corrupt_prepared_pair_is_not_recovered_as_publication(writable):
    project, registry, instance, content, image = writable
    def interrupt(stage):
        if stage == "prepared":
            raise RuntimeError("interrupted")
    with pytest.raises(RuntimeError):
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True, interrupt=interrupt)
    row = publication_status(project, registry)[0]
    stage = project / row["details"]["stage"]
    (stage / "new.png").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        recover_publication(project, registry, row["operation_id"], approved=True)
    assert recover_publication(project, registry, row["operation_id"], approved=True, action="rollback")["state"] == "rolled_back"
    assert resolve_silhouette(project, instance, registry=registry)["layout"] == "legacy-v1"


@pytest.mark.parametrize("stage", ["run-prepared", "run-installed"])
def test_run_publication_interruption_and_identical_retry(writable, stage):
    project, registry, instance, content, image = writable
    draft = _engraving_draft(writable)
    payload = {key: draft[key] for key in ("source_instance_id", "mode", "generation", "source")}
    def interrupt(current):
        if current == stage:
            raise RuntimeError("interrupted run")
    with pytest.raises(RuntimeError):
        publish_generation_run(project, registry, instance, "objects", "field", "openai", 2, payload,
                               {"input.png": image}, approved=True, interrupt=interrupt)
    binding = locations.registered_instance(project, registry, instance)
    assert next(run for run in binding["runs"] if run["number"] == 2)["state"] == "prepared"
    result = publish_generation_run(project, registry, instance, "objects", "field", "openai", 2, payload, {"input.png": image}, approved=True)
    assert result["published"]
    assert not publish_generation_run(project, registry, instance, "objects", "field", "openai", 2, payload, {"input.png": image}, approved=True)["published"]
    payload["generation"] = {**payload["generation"], "prompt": "different run"}
    with pytest.raises(ValueError, match="reserved"):
        publish_generation_run(project, registry, instance, "objects", "field", "openai", 2, payload, {"input.png": image}, approved=True)


def test_generation_run_cannot_contain_publication_sidecar(writable):
    project, registry, instance, content, image = writable
    draft = _engraving_draft(writable)
    payload = {key: draft[key] for key in ("source_instance_id", "mode", "generation", "source")}
    with pytest.raises(ValueError, match="cannot become"):
        publish_generation_run(project, registry, instance, "objects", "field", "openai", 2, payload, {"engraving-frame.json": image}, approved=True)


def test_concurrent_next_run_reservation_never_recycles(writable):
    from concurrent.futures import ThreadPoolExecutor
    project, registry, instance, content, image = writable
    store = ResourceStore(project, registry, approved=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(store.reserve_generation_run, instance, "objects", "field", "local", "frame") for _ in range(4)]
        reservations = [job.result(timeout=10) for job in jobs]
    assert sorted(item["number"] for item in reservations) == [1, 2, 3, 4]
    store.finish_generation_attempt(reservations[0], "failed")
    store.finish_generation_attempt(reservations[1], "abandoned")
    assert store.reserve_generation_run(instance, "objects", "field", "local", "isolated")["number"] == 5


@pytest.mark.parametrize("change", [{"field": ""}, {"label": ""}, {"field": "animals"}, {"frame": 21}])
def test_legacy_publication_rejects_invalid_new_active_pair(writable, change):
    from services.resource_publication import publish_silhouette
    project, registry, instance, content, image = writable
    path = project / "data/silhouettes/catalog/movie/film/field/object_0002.json"
    with pytest.raises(ValueError):
        publish_silhouette(project, {**content, **change}, image, source_path=path)
    assert not path.exists() and not path.with_suffix(".png").exists()
    assert publication_status(project, registry) == []


def test_registered_source_lookup_is_exact_and_does_not_allocate(writable):
    from services.resource_resolution import resolve_source_silhouette
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    before = registry.read_bytes()
    old_path = project / "data/silhouettes/catalog/movie/film/field/object_0001.json"
    source = resolve_source_silhouette(project, old_path, registry=registry)
    assert source["instance_id"] == instance and source["layout"] == "media-first-v1"
    assert registry.read_bytes() == before


def test_registered_annotation_loader_preserves_committed_arrays_and_json(writable):
    import numpy as np
    from data.index import load_annotation_items, load_embeddings, load_manifest
    from services.frame_match import load_best_frame_lookup
    project, registry, instance, content, image = writable
    record = locations.find_media(project, "movie", media_id="tmdb_1")
    array = locations.annotation_path(project, "film.mp4", "movie", ".annotations.npy")
    np.save(array, np.ones((1, 2), dtype=np.float32))
    manifest = locations.annotation_path(project, "film.mp4", "movie", ".annotations.manifest.json")
    manifest.write_text(json.dumps({"fixture": True}))
    ResourceStore(project, registry, approved=True).commit_annotation_layout(record, "movie", "legacy-v1")
    before = array.read_bytes(), manifest.read_bytes(), registry.read_bytes()
    assert load_annotation_items(project, "film.mp4", "movie", registry=registry)[0]["shot"]["annotation"]["objects"] == ["field"]
    assert load_embeddings(project, "film.mp4", "movie", registry=registry).shape == (1, 2)
    assert load_manifest(project, "film.mp4", "movie", registry=registry) == {"fixture": True}
    assert load_best_frame_lookup(project, "film.mp4", registry=registry) == {}
    assert before == (array.read_bytes(), manifest.read_bytes(), registry.read_bytes())


def test_reserved_run_requires_ownership_and_completed_run_is_immutable(writable):
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    store = ResourceStore(project, registry, approved=True)
    reservation = store.reserve_generation_run(instance, "objects", "field", "local", "isolated")
    generation = {"service": "fixture", "model": "fixture", "model_version": "1", "prompt": "field", "parameters": {}, "software": {"fixture": "1"}, "seed": 1}
    payload = {"source_instance_id": instance, "mode": "isolated", "generation": generation,
               "source": {"image_sha256": digest(resolve_silhouette(project, instance, registry=registry)["image"])}}
    with pytest.raises(ValueError, match="ownership"):
        publish_generation_run(project, registry, instance, "objects", "field", "local", reservation["number"], payload, {"image.png": image}, approved=True)
    result = publish_generation_run(project, registry, instance, "objects", "field", "local", reservation["number"], payload, {"image.png": image}, approved=True, reservation_token=reservation["token"])
    before = result["run"].read_bytes()
    with pytest.raises(ValueError):
        store.finish_generation_attempt(reservation, "failed")
    assert result["run"].read_bytes() == before


def test_engraving_status_is_pure_even_for_interrupted_metadata(writable):
    from services.engraving_paths import engraving_paths, engraving_status
    project, registry, instance, content, image = writable
    source = project / "data/silhouettes/catalog/movie/film/field/object_0001.json"
    paths = engraving_paths(project, source, content, "frame")
    paths["dir"].mkdir(parents=True)
    paths["metadata"].write_text(json.dumps({"status": "generating", "mode": "frame"}))
    before = paths["metadata"].read_bytes()
    assert engraving_status(project, source, content, "frame") == "generating"
    assert paths["metadata"].read_bytes() == before


def test_annotation_commitment_rejects_missing_declared_field_without_repair(writable):
    project, registry, instance, content, image = writable
    path = locations.annotation_path(project, "film.mp4", "movie")
    entries = json.loads(path.read_text())
    entries[0]["shot"]["annotation"]["classified"] = [{"label": "field"}]
    path.write_text(json.dumps(entries))
    before = path.read_bytes()
    record = locations.find_media(project, "movie", media_id="tmdb_1")
    with pytest.raises(ValueError, match="classification validation failed"):
        ResourceStore(project, registry, approved=True).commit_annotation_layout(record, "movie", "legacy-v1")
    assert locations.annotation_commitment(project, "film.mp4", "movie", registry) is None
    assert path.read_bytes() == before


def test_metadata_read_does_not_repair(tmp_path):
    path = tmp_path / "engraving.json"
    path.write_text('{"status":"generating"}')
    before = path.read_bytes()
    assert read_engraving_meta(path)["status"] == "generating"
    assert path.read_bytes() == before


def test_metadata_repair_requires_approval(tmp_path):
    path = tmp_path / "engraving.json"
    path.write_text('{"status":"generating"}')
    with pytest.raises(ValueError, match="approval"):
        repair_engraving_meta(path)
    assert repair_engraving_meta(path, approved=True)["status"] == "failed"
    assert json.loads(path.read_text())["status"] == "failed"