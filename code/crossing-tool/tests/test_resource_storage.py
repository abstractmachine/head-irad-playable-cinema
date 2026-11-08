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


def test_collection_journal_supports_media_without_instance(writable):
    from data.resource_storage import upgrade_collection_journal
    project, registry, instance, content, image = writable
    with pytest.raises(ValueError, match="approval"):
        upgrade_collection_journal(project, registry)
    assert upgrade_collection_journal(project, registry, approved=True) == {"upgraded": False}
    with ResourceStore(project, registry, approved=True).transaction() as database:
        database.execute("INSERT INTO publication_operations VALUES(?,?,?,?,?,?)",
                         ("collection-probe", None, "annotation-collection", "", "finished", "{}"))


@pytest.mark.parametrize("state", ["finished", "prepared"])
def test_existing_collection_journal_upgrade_preserves_rows(writable, state):
    from data.resource_storage import upgrade_collection_journal
    project, registry, instance, content, image = writable
    with sqlite3.connect(registry) as database:
        schema = database.execute("SELECT sql FROM sqlite_master WHERE name='publication_operations'").fetchone()[0]
        database.execute("DROP TABLE publication_operations")
        database.execute(schema.replace("instance_id TEXT REFERENCES", "instance_id TEXT NOT NULL REFERENCES"))
        database.execute("CREATE UNIQUE INDEX one_pending_publication ON publication_operations(instance_id,kind,mode) WHERE state NOT IN ('finished','rolled_back')")
        database.execute("INSERT INTO publication_operations VALUES(?,?,?,?,?,?)", ("existing", instance, "silhouettes", "", state, '{"evidence":"retained"}'))
    if state == "prepared":
        with pytest.raises(ValueError, match="recovery"):
            upgrade_collection_journal(project, registry, approved=True)
    else:
        assert upgrade_collection_journal(project, registry, approved=True) == {"upgraded": True}
    with sqlite3.connect(registry) as database:
        assert database.execute("SELECT * FROM publication_operations").fetchone() == ("existing", instance, "silhouettes", "", state, '{"evidence":"retained"}')
        assert next(row for row in database.execute("PRAGMA table_info(publication_operations)") if row[1] == "instance_id")[3] == int(state == "prepared")


def test_registered_layout_cannot_bypass_publication(writable):
    project, registry, instance, content, image = writable
    store = ResourceStore(project, registry, approved=True)
    media = locations.find_media(project, "movie", filename="film.mp4")
    store.commit_annotation_layout(media, "movie", "legacy-v1")
    with pytest.raises(ValueError, match="controlled artifact publication"):
        store.commit_annotation_layout(media, "movie", "legacy-v1", expected_revision=1)
    locations.annotation_path(project, "film.mp4", "movie").write_text("[]")
    with pytest.raises(ValueError, match="controlled artifact publication"):
        store.commit_annotation_layout(media, "movie", "legacy-v1", expected_revision=1)


def test_registered_annotation_worker_passes_loaded_commitment(writable, monkeypatch, resource_app):
    from visualizers.shot_visualizer import AnnotateWorker
    project, registry, instance, content, image = writable
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(locations.find_media(project, "movie", filename="film.mp4"), "movie", "legacy-v1")
    received = []
    monkeypatch.setattr("data.annotate.annotate_file_shots", lambda **kwargs: received.append(kwargs) or {"updated": 1})
    worker = AnnotateWorker(project, "film.mp4", "movie", "fixture", 1, registry=registry, approved=True, commitment=commitment)
    worker.run()
    assert received[0]["registry"] == registry and received[0]["approved"]
    assert received[0]["expected_revision"] == 1 and received[0]["expected_hashes"] == commitment["artifacts"]
    assert received[0]["on_shot_done"] is None
    worker.deleteLater()
    from PyQt5.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_actual_cli_registered_descriptive_metadata(writable, monkeypatch):
    import cli
    from data.metadata import MOVIE_REQUIRED
    project, registry, instance, content, image = writable
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: str(project) if key == "path" else default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: None)
    record = {field: "descriptive" for field in MOVIE_REQUIRED}
    record.update(filename="film.mp4", media_id="tmdb_1", title="Edited title", year=1970, runtime=90)
    arguments = ["metadata", "--resource-registry", str(registry), "set", json.dumps(record)]
    with pytest.raises(ValueError, match="approval"):
        parsed = cli.build_parser().parse_args(arguments)
        parsed.func(parsed)
    parsed = cli.build_parser().parse_args(arguments + ["--approved-resource-edit"])
    parsed.func(parsed)
    assert locations.find_media(project, "movie", media_id="tmdb_1")["title"] == "Edited title"
    before = (project / "data/metadata/movie.json").read_bytes()
    record["media_id"] = "tmdb_2"
    parsed = cli.build_parser().parse_args(["metadata", "--resource-registry", str(registry), "set", json.dumps(record), "--approved-resource-edit"])
    with pytest.raises(ValueError, match="association"):
        parsed.func(parsed)
    assert (project / "data/metadata/movie.json").read_bytes() == before


def test_actual_cli_registered_frame_publication(writable, monkeypatch):
    import cli
    import numpy as np
    from services.frame_embeddings import commit_frame_artifacts
    project, registry, instance, content, image = writable
    media = locations.find_media(project, "movie", filename="film.mp4")
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    video = project / "media/videos/movie/film.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"inert resolver fixture")
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: str(project) if key == "path" else default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: None)
    received = []
    def precomputed(**kwargs):
        received.append(kwargs)
        commit_frame_artifacts(project, kwargs["filename"], kwargs["media_type"], np.ones((1, 2), dtype="float32"), np.ones(1, dtype=bool),
                               registry=kwargs["registry"], approved=kwargs["approved"], expected_revision=kwargs["expected_revision"], expected_hashes=kwargs["expected_hashes"])
        return {"status": "ok", "valid_count": 1, "item_count": 1}
    monkeypatch.setattr("services.frame_embeddings.build_frame_embeddings", precomputed)
    parsed = cli.build_parser().parse_args(["index", "frame-embeddings", "--title", "film.mp4", "--resource-registry", str(registry), "--approved-resource-edit"])
    parsed.func(parsed)
    assert received[0]["expected_hashes"] == commitment["artifacts"]
    assert locations.annotation_commitment(project, "film.mp4", "movie", registry)["revision"] == 2
    assert np.load(locations.resolve_annotation(project, "film.mp4", "movie", ".frames.npy", registry=registry)).shape == (1, 2)


def test_simultaneous_registered_annotation_writers_conflict(writable):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from data.annotate import write_annotation_aggregate
    project, registry, instance, content, image = writable
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(locations.find_media(project, "movie", filename="film.mp4"), "movie", "legacy-v1")
    barrier = Barrier(2)
    def write(description):
        entries = [{"shot": {"shot_id": content["shot_id"], "annotation": {"objects": ["field"], "description": description}}}]
        barrier.wait(timeout=10)
        try:
            write_annotation_aggregate(project, "film.mp4", "movie", entries, registry=registry, approved=True,
                                       expected_revision=1, expected_hashes=commitment["artifacts"])
            return "committed"
        except ValueError as exc:
            assert "conflict" in str(exc).lower()
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write, description) for description in ("first", "second")]
        assert sorted(future.result(timeout=20) for future in futures) == ["committed", "conflict"]
    assert locations.annotation_commitment(project, "film.mp4", "movie", registry)["revision"] == 2
    with sqlite3.connect(registry) as database:
        assert database.execute("SELECT count(*) FROM publication_operations WHERE kind='annotation-collection'").fetchone()[0] == 1


def _annotation_producer_fixture(request, media_type, monkeypatch):
    from PIL import Image
    from data.shotlist import write_shotlist
    import cli
    if media_type == "movie":
        project, registry, instance, content, image = request.getfixturevalue("writable")
        publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
        annotation = locations.annotation_path(project, content["filename"], media_type)
        entries = json.loads(annotation.read_text())
        entries[0]["shot"]["best_frame"] = {"frame": 12, "score": 0.8, "method": "semantic"}
        annotation.write_text(json.dumps(entries))
        ResourceStore(project, registry, approved=True).commit_annotation_layout(locations.find_media(project, media_type, filename=content["filename"]), media_type, "legacy-v1")
    else:
        project, registry, instance, content, annotation = request.getfixturevalue("registered_gameplay")
    filename = content["filename"]
    video = project / "media/videos" / media_type / filename
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"inert resolver fixture")
    write_shotlist(project, filename, media_type, [{"shot_id": content["shot_id"], "start_frame": 10, "end_frame": 20,
                                                  "start_time": "00:00:00", "end_time": "00:00:01", "Scene": "1"}])
    frame = project / "sample.png"
    Image.new("RGB", (4, 4), (30, 60, 90)).save(frame)
    mapping = project / "preferences/data/mapping.yaml"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("mapping:\n  fields: [objects]\n  include_labels: true\n")
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: str(project) if key == "path" else default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: None)
    monkeypatch.setattr("data.annotate.load_system_prompt", lambda *args: ("fixture system", "fixture-prompt"))
    monkeypatch.setattr("data.annotate.load_user_prompt", lambda *args: ("$title $shot_index", "fixture-user"))
    monkeypatch.setattr("data.annotate._find_prebaked_frames", lambda *args: [str(frame)])
    monkeypatch.setattr("data.annotate._load_text_generation_pipeline", lambda *args: object())
    monkeypatch.setattr(cli, "_auto_index_and_motif", lambda *args, **kwargs: pytest.fail("Registered CLI fell through to legacy follow-ups"))
    monkeypatch.setattr("services.frame_match.annotate_best_frames", lambda *args, **kwargs: pytest.fail("Annotation CLI recalculated BEST"))
    return project, registry, instance, content, annotation


@pytest.mark.parametrize("media_type", ["movie", "gameplay"])
@pytest.mark.parametrize("mode", ["manual", "generate", "batch"])
def test_actual_registered_annotation_cli_body(request, monkeypatch, media_type, mode):
    import cli
    manual = mode == "manual"
    all_files = mode == "batch"
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, media_type, monkeypatch)
    previous = json.loads(annotation.read_text())[0]["shot"]["best_frame"]
    response = {"description": "generated description", "objects": [content["label"]],
                "classification": {"field": "objects", "label": content["label"]}}
    calls = []
    def model(*args, **kwargs):
        calls.append(kwargs)
        return json.dumps(response), json.dumps(response), "fixture device"
    monkeypatch.setattr("data.annotate._call_model", model)
    arguments = ["annotate", "shot", content["filename"], "0", "--media", media_type, "--resource-registry", str(registry), "--approved-resource-edit"]
    if manual:
        arguments += ["--manual", "manual description"]
    if all_files and not manual:
        arguments += ["--all"]
    parsed = cli.build_parser().parse_args(arguments)
    result = parsed.func(parsed)
    if all_files and not manual:
        result = result[0]["commitment"]
    current = json.loads(annotation.read_text())[0]
    assert current["shot"]["annotation"]["description"] == ("manual description" if manual else "generated description")
    assert current["shot"]["best_frame"] == previous and current["shot"]["shot_id"] == content["shot_id"]
    assert result["revision"] == 2 and result["artifacts"][".annotations.json"] == digest(annotation)
    assert len(calls) == int(not manual)
    if manual:
        assert current["annotation"]["edit_service"] == "manual"
    if not manual:
        assert current["annotation"]["generation_parameters"]["do_sample"] is False
        assert current["annotation"]["sampled_frame_count"] == 1
    assert resolve_silhouette(project, instance, registry=registry)["record"]["source_validation"]["dirty"]
    assert not locations.annotation_path(project, content["filename"], media_type, layout="legacy-v1" if media_type == "gameplay" else "media-first-v1").exists()


@pytest.mark.parametrize("classification", [{"label": "field"}, {"field": "objects"}, {"field": "animals", "label": "field"}])
def test_generation_body_rejects_invalid_classification(request, monkeypatch, classification):
    from data.annotate import annotate_file_shots
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, "movie", monkeypatch)
    before = annotation.read_bytes()
    response = json.dumps({"description": "bad result", "objects": ["field"], "classification": classification})
    monkeypatch.setattr("data.annotate._call_model", lambda *args, **kwargs: (response, response, "fixture"))
    commitment = locations.annotation_commitment(project, content["filename"], "movie", registry)
    with pytest.raises(RuntimeError, match="no successful annotations"):
        annotate_file_shots(project, content["filename"], registry=registry, approved=True,
                            expected_revision=1, expected_hashes=commitment["artifacts"])
    assert annotation.read_bytes() == before
    assert locations.annotation_commitment(project, content["filename"], "movie", registry)["revision"] == 1


@pytest.mark.parametrize("media_type", ["movie", "gameplay"])
@pytest.mark.parametrize("all_files", [False, True])
def test_actual_grouped_annotation_index_cli(request, monkeypatch, media_type, all_files):
    import cli
    import numpy as np
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, media_type, monkeypatch)
    source = annotation.read_bytes()
    monkeypatch.setattr("data.index.embed_texts", lambda lines, *args: np.full((len(lines), 3), 0.5, dtype="float32"))
    arguments = ["index", "process", "--media", media_type, "--resource-registry", str(registry), "--approved-resource-edit"]
    arguments += ["--all"] if all_files else ["--title", content["filename"]]
    parsed = cli.build_parser().parse_args(arguments)
    parsed.func(parsed)
    commitment = locations.annotation_commitment(project, content["filename"], media_type, registry)
    assert commitment["revision"] == 2 and annotation.read_bytes() == source
    manifest_path = locations.resolve_annotation(project, content["filename"], media_type, ".annotations.manifest.json", registry=registry)
    manifest = json.loads(manifest_path.read_text())
    for section, suffix in (("json", ".annotations.json"), ("txt", ".annotations.txt"), ("npy", ".annotations.npy")):
        path = locations.resolve_annotation(project, content["filename"], media_type, suffix, registry=registry)
        assert manifest[section]["hash"] == "sha256:" + digest(path)
    assert manifest["npy"]["shape"] == [1, 3] and ".frames.npy" not in commitment["artifacts"]
    parsed.func(parsed)
    assert locations.annotation_commitment(project, content["filename"], media_type, registry)["revision"] == 2


@pytest.mark.parametrize("boundary", [".annotations.txt-installed", ".annotations.npy-installed", ".annotations.manifest.json-installed", "committed"])
def test_grouped_annotation_producer_recovery(request, monkeypatch, boundary):
    import numpy as np
    from data.index import rebuild_annotation_index
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, "movie", monkeypatch)
    monkeypatch.setattr("data.index.embed_texts", lambda lines, *args: np.ones((len(lines), 2), dtype="float32"))
    commitment = locations.annotation_commitment(project, content["filename"], "movie", registry)
    def interrupt(stage):
        if stage == boundary:
            raise RuntimeError("interrupted producer")
    with pytest.raises(RuntimeError, match="interrupted producer"):
        rebuild_annotation_index(project, content["filename"], "movie", "fixture", registry=registry, approved=True,
                                 expected_revision=1, expected_hashes=commitment["artifacts"], operation_id="groupedproducer", interrupt=interrupt)
    with pytest.raises(ValueError, match="recovery"):
        locations.resolve_annotation(project, content["filename"], "movie", registry=registry)
    monkeypatch.setattr("data.index.embed_texts", lambda *args: pytest.fail("Retry invoked embedding inference"))
    rebuild_annotation_index(project, content["filename"], "movie", "fixture", registry=registry, approved=True,
                             expected_revision=1, expected_hashes=commitment["artifacts"], operation_id="groupedproducer")
    revision = resolve_silhouette(project, instance, registry=registry)["revision"]
    rebuild_annotation_index(project, content["filename"], "movie", "fixture", registry=registry, approved=True,
                             expected_revision=1, expected_hashes=commitment["artifacts"], operation_id="groupedproducer")
    assert locations.annotation_commitment(project, content["filename"], "movie", registry)["revision"] == 2
    assert resolve_silhouette(project, instance, registry=registry)["revision"] == revision
    manifest = json.loads(locations.resolve_annotation(project, content["filename"], "movie", ".annotations.manifest.json", registry=registry).read_text())
    array = locations.resolve_annotation(project, content["filename"], "movie", ".annotations.npy", registry=registry)
    assert manifest["npy"]["hash"] == "sha256:" + digest(array)


def test_generation_producer_concurrent_conflict(request, monkeypatch):
    from data.annotate import annotate_file_shots, annotate_manual
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, "movie", monkeypatch)
    commitment = locations.annotation_commitment(project, content["filename"], "movie", registry)
    def model(*args, **kwargs):
        annotate_manual(project, content["filename"], "movie", [content["shot_id"]], "winning manual edit", registry=registry,
                        approved=True, expected_revision=1, expected_hashes=commitment["artifacts"])
        response = json.dumps({"description": "stale generated edit", "objects": ["field"]})
        return response, response, "fixture"
    monkeypatch.setattr("data.annotate._call_model", model)
    with pytest.raises(RuntimeError, match="conflict"):
        annotate_file_shots(project, content["filename"], registry=registry, approved=True, expected_revision=1, expected_hashes=commitment["artifacts"])
    assert json.loads(annotation.read_text())[0]["shot"]["annotation"]["description"] == "winning manual edit"
    assert locations.annotation_commitment(project, content["filename"], "movie", registry)["revision"] == 2


def test_grouped_producer_rejects_mapping_drift(request, monkeypatch):
    import numpy as np
    from data.index import rebuild_annotation_index
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, "movie", monkeypatch)
    commitment = locations.annotation_commitment(project, content["filename"], "movie", registry)
    def embed(lines, *args):
        (project / "preferences/data/mapping.yaml").write_text("mapping:\n  fields: [description]\n")
        return np.ones((len(lines), 2), dtype="float32")
    monkeypatch.setattr("data.index.embed_texts", embed)
    with pytest.raises(ValueError, match="mapping changed"):
        rebuild_annotation_index(project, content["filename"], "movie", "fixture", registry=registry, approved=True,
                                 expected_revision=1, expected_hashes=commitment["artifacts"])
    assert locations.annotation_commitment(project, content["filename"], "movie", registry)["revision"] == 1
    assert not locations.annotation_path(project, content["filename"], "movie", ".annotations.npy", registry=registry).exists()


def test_generation_source_change_makes_grouped_arrays_stale(request, monkeypatch):
    import numpy as np
    from data.index import rebuild_annotation_index, load_embeddings
    from data.annotate import annotate_file_shots
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, "movie", monkeypatch)
    commitment = locations.annotation_commitment(project, content["filename"], "movie", registry)
    monkeypatch.setattr("data.index.embed_texts", lambda lines, *args: np.ones((len(lines), 2), dtype="float32"))
    rebuild_annotation_index(project, content["filename"], "movie", "fixture", registry=registry, approved=True,
                             expected_revision=1, expected_hashes=commitment["artifacts"])
    commitment = locations.annotation_commitment(project, content["filename"], "movie", registry)
    response = json.dumps({"description": "new source", "objects": ["field"]})
    monkeypatch.setattr("data.annotate._call_model", lambda *args, **kwargs: (response, response, "fixture"))
    summary = annotate_file_shots(project, content["filename"], registry=registry, approved=True,
                                  expected_revision=2, expected_hashes=commitment["artifacts"])
    assert summary["commitment"]["revision"] == 3
    with pytest.raises(ValueError, match="(?i)stale"):
        load_embeddings(project, content["filename"], "movie", registry=registry)


@pytest.mark.parametrize("option", ["--log", "--with-frame", "--export-csv"])
def test_registered_cli_unsupported_side_effects_refuse_before_mutation(request, monkeypatch, option):
    import cli
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, "movie", monkeypatch)
    before = annotation.read_bytes()
    monkeypatch.setattr("data.annotate._load_text_generation_pipeline", lambda *args: pytest.fail("Unsupported option loaded a model"))
    arguments = ["annotate", "shot", content["filename"], "--resource-registry", str(registry), "--approved-resource-edit", option]
    if option == "--export-csv":
        arguments.append(str(project / "data/unsafe.csv"))
    parsed = cli.build_parser().parse_args(arguments)
    with pytest.raises(SystemExit):
        parsed.func(parsed)
    assert annotation.read_bytes() == before and locations.annotation_commitment(project, content["filename"], "movie", registry)["revision"] == 1


def test_manual_cli_legacy_caption_behavior(request, monkeypatch):
    import cli
    from data.shotlist import read_shotlist
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, "movie", monkeypatch)
    before = annotation.read_bytes()
    parsed = cli.build_parser().parse_args(["annotate", "shot", content["filename"], "0", "--manual", "legacy caption"])
    parsed.func(parsed)
    assert read_shotlist(project, content["filename"], "movie")[0]["Shot_Caption"] == "legacy caption"
    assert annotation.read_bytes() == before


@pytest.mark.parametrize("manual", [True, False])
def test_actual_registered_scene_cli(request, monkeypatch, manual):
    import cli
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, "movie", monkeypatch)
    response = json.dumps({"description": "scene generated", "objects": ["field"]})
    monkeypatch.setattr("data.annotate._call_model", lambda *args, **kwargs: (response, response, "fixture"))
    arguments = ["annotate", "scene", content["filename"], "1", "--resource-registry", str(registry), "--approved-resource-edit"]
    if manual:
        arguments += ["--manual", "scene manual"]
    parsed = cli.build_parser().parse_args(arguments)
    result = parsed.func(parsed)
    assert result["revision"] == 2
    assert json.loads(annotation.read_text())[0]["shot"]["annotation"]["description"] == ("scene manual" if manual else "scene generated")


def test_generation_body_interrupted_commit_recovers(request, monkeypatch):
    from data.annotate import annotate_file_shots
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, "movie", monkeypatch)
    response = json.dumps({"description": "recoverable generated result", "objects": ["field"]})
    monkeypatch.setattr("data.annotate._call_model", lambda *args, **kwargs: (response, response, "fixture"))
    commitment = locations.annotation_commitment(project, content["filename"], "movie", registry)
    def interrupt(stage):
        if stage == ".annotations.json-installed":
            raise RuntimeError("source interrupted")
    with pytest.raises(RuntimeError, match="source interrupted"):
        annotate_file_shots(project, content["filename"], registry=registry, approved=True, expected_revision=1,
                            expected_hashes=commitment["artifacts"], operation_id="generationproducer", interrupt=interrupt)
    with pytest.raises(ValueError, match="recovery"):
        locations.resolve_annotation(project, content["filename"], "movie", registry=registry)
    monkeypatch.setattr("data.annotate._call_model", lambda *args, **kwargs: pytest.fail("Recovery invoked annotation inference"))
    recover_publication(project, registry, "generationproducer", approved=True)
    recover_publication(project, registry, "generationproducer", approved=True)
    assert json.loads(annotation.read_text())[0]["shot"]["annotation"]["description"] == "recoverable generated result"
    assert locations.annotation_commitment(project, content["filename"], "movie", registry)["revision"] == 2
    assert resolve_silhouette(project, instance, registry=registry)["revision"] == 2


def test_automatic_cli_legacy_behavior(request, monkeypatch):
    import cli
    project, registry, instance, content, annotation = _annotation_producer_fixture(request, "movie", monkeypatch)
    response = json.dumps({"description": "legacy generated", "objects": ["field"]})
    monkeypatch.setattr("data.annotate._call_model", lambda *args, **kwargs: (response, response, "fixture"))
    followups = []
    monkeypatch.setattr(cli, "_auto_index_and_motif", lambda *args, **kwargs: followups.append(args))
    parsed = cli.build_parser().parse_args(["annotate", "shot", content["filename"], "0", "--no-best"])
    parsed.func(parsed)
    assert json.loads(annotation.read_text())[0]["shot"]["annotation"]["description"] == "legacy generated"
    assert followups
    with sqlite3.connect(registry) as database:
        assert database.execute("SELECT revision FROM annotation_layouts").fetchone()[0] == 1


def test_valid_silhouette_publication(writable):
    project, registry, instance, content, image = writable
    assert publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)["revision"] == 1
    result = resolve_silhouette(project, instance, registry=registry)
    assert result["record"]["field"] == "objects" and result["record"]["label"] == "field"


@pytest.mark.parametrize("boundary", [None, "prepared", ".annotations.json-installed", "before-commit", "committed", "invalidated", "image-installed", "metadata-installed", "dependent-completed"])
def test_controlled_annotation_write_and_resume(writable, boundary):
    from data.annotate import write_annotation_aggregate
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    media = locations.find_media(project, "movie", filename="film.mp4")
    before = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    entries = [{"shot": {"shot_id": content["shot_id"], "annotation": {"objects": ["field"], "description": "edited"}}}]
    def interrupt(current):
        if current == boundary:
            raise RuntimeError("interrupted")
    arguments = dict(registry=registry, approved=True, expected_revision=before["revision"],
                     expected_hashes=before["artifacts"], operation_id="sourceedit")
    if boundary:
        with pytest.raises(RuntimeError, match="interrupted"):
            write_annotation_aggregate(project, "film.mp4", "movie", entries, interrupt=interrupt, **arguments)
        if boundary in (".annotations.json-installed", "before-commit"):
            with pytest.raises(ValueError, match="recovery"):
                locations.resolve_annotation(project, "film.mp4", "movie", registry=registry)
    write_annotation_aggregate(project, "film.mp4", "movie", entries, **arguments)
    assert locations.annotation_commitment(project, "film.mp4", "movie", registry)["revision"] == 2
    current = resolve_silhouette(project, instance, registry=registry)
    assert current["record"]["source_validation"]["dirty"]
    assert current["image"].read_bytes() == image.read_bytes()
    assert current["revision"] == 2


@pytest.mark.parametrize("annotation", [
    {"objects": ["field"], "classification": {"label": "field"}},
    {"objects": ["field"], "classification": {"field": "objects"}},
    {"objects": ["field"], "classification": {"field": "animals", "label": "field"}},
    {"objects": ["field"], "classification": {"field": "objects", "label": "field"}},
])
def test_registered_annotation_classification(writable, annotation):
    from data.annotate import write_annotation_aggregate
    project, registry, instance, content, image = writable
    media = locations.find_media(project, "movie", filename="film.mp4")
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    entries = [{"shot": {"shot_id": content["shot_id"], "annotation": annotation}}]
    arguments = dict(registry=registry, approved=True, expected_revision=1, expected_hashes=commitment["artifacts"])
    valid = annotation["classification"] == {"field": "objects", "label": "field"}
    if valid:
        write_annotation_aggregate(project, "film.mp4", "movie", entries, **arguments)
    else:
        with pytest.raises(ValueError, match="classification"):
            write_annotation_aggregate(project, "film.mp4", "movie", entries, **arguments)
    assert locations.annotation_commitment(project, "film.mp4", "movie", registry)["revision"] == 1 + int(valid)


@pytest.mark.parametrize("frame", [10, 19, 20, 21, -1, True])
def test_registered_best_frame_owner(writable, frame):
    from services.frame_match import commit_best_frame
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    media = locations.find_media(project, "movie", filename="film.mp4")
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    arguments = dict(registry=registry, approved=True, expected_revision=1, expected_hashes=commitment["artifacts"])
    if type(frame) is int and 10 <= frame <= 20:
        commit_best_frame(project, "film.mp4", "movie", content["shot_id"], {"frame": frame, "source": "user"}, **arguments)
        current = resolve_silhouette(project, instance, registry=registry)
        assert current["instance_id"] == instance
        assert current["record"]["source_validation"]["dirty"]
        assert current["image"].read_bytes() == image.read_bytes()
    else:
        with pytest.raises(ValueError, match="source frame"):
            commit_best_frame(project, "film.mp4", "movie", content["shot_id"], {"frame": frame}, **arguments)


@pytest.mark.parametrize("boundary", [None, ".frames.npy-installed", ".frames.valid.npy-installed", "before-commit", "committed"])
def test_registered_frame_artifacts(writable, boundary):
    import numpy as np
    from services.frame_embeddings import commit_frame_artifacts
    project, registry, instance, content, image = writable
    media = locations.find_media(project, "movie", filename="film.mp4")
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    def interrupt(current):
        if current == boundary:
            raise RuntimeError("interrupted")
    arguments = dict(registry=registry, expected_revision=1, expected_hashes=commitment["artifacts"], approved=True)
    if boundary:
        with pytest.raises(RuntimeError):
            commit_frame_artifacts(project, "film.mp4", "movie", np.ones((1, 3), dtype="float32"), np.ones(1, dtype=bool),
                                   interrupt=interrupt, operation_id="frameupdate", **arguments)
        recover_publication(project, registry, "frameupdate", approved=True)
        commit_frame_artifacts(project, "film.mp4", "movie", np.ones((1, 3), dtype="float32"), np.ones(1, dtype=bool),
                       operation_id="frameupdate", **arguments)
    else:
        commit_frame_artifacts(project, "film.mp4", "movie", np.ones((1, 3), dtype="float32"), np.ones(1, dtype=bool), **arguments)
    assert locations.annotation_commitment(project, "film.mp4", "movie", registry)["revision"] == 2
    path = locations.resolve_annotation(project, "film.mp4", "movie", ".frames.npy", registry=registry)
    assert np.load(path).shape == (1, 3)


@pytest.mark.parametrize("boundary", [".annotations.json-installed", "committed", "dependent-completed"])
def test_best_frame_retry_reuses_committed_edit(writable, boundary):
    from services.frame_match import commit_best_frame
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    media = locations.find_media(project, "movie", filename="film.mp4")
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    arguments = dict(registry=registry, approved=True, expected_revision=1, expected_hashes=commitment["artifacts"], operation_id="bestframeedit")
    def interrupt(current):
        if current == boundary:
            raise RuntimeError("interrupted")
    assignment = {"frame": 15, "source": "user"}
    with pytest.raises(RuntimeError):
        commit_best_frame(project, "film.mp4", "movie", content["shot_id"], assignment, interrupt=interrupt, **arguments)
    commit_best_frame(project, "film.mp4", "movie", content["shot_id"], assignment, **arguments)
    commit_best_frame(project, "film.mp4", "movie", content["shot_id"], assignment, **arguments)
    assert locations.annotation_commitment(project, "film.mp4", "movie", registry)["revision"] == 2
    assert resolve_silhouette(project, instance, registry=registry)["revision"] == 2


def test_registered_cli_annotation_repair(writable, monkeypatch):
    import cli
    project, registry, instance, content, image = writable
    video = project / "media/videos/movie/film.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"inert resolver fixture")
    path = locations.annotation_path(project, "film.mp4", "movie")
    path.write_text(json.dumps([{"shot": {"shot_id": content["shot_id"], "annotation": {"objects": "field, wagon"}}}]))
    media = locations.find_media(project, "movie", filename="film.mp4")
    ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: str(project) if key == "path" else default)
    monkeypatch.setattr(cli.prefs, "set", lambda *arguments: None)
    arguments = cli.build_parser().parse_args(["annotate", "validate", "film", "--media", "movie",
                                               "--resource-registry", str(registry), "--approved-resource-edit"])
    arguments.func(arguments)
    assert locations.annotation_commitment(project, "film.mp4", "movie", registry)["revision"] == 2
    assert json.loads(path.read_text())[0]["shot"]["annotation"]["objects"] == ["field", "wagon"]


def test_shot_best_frame_actual_persistence(writable, monkeypatch):
    from types import SimpleNamespace
    from visualizers.shot_visualizer import ShotlistVisualizer, QMessageBox
    project, registry, instance, content, image = writable
    media = locations.find_media(project, "movie", filename="film.mp4")
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    monkeypatch.setattr(QMessageBox, "critical", lambda *arguments: pytest.fail(str(arguments)))
    fake = SimpleNamespace(project_path=project, filename="film.mp4", media_type="movie", _resource_registry=registry,
                           _resource_edit_approved=True, _annotation_commitment=commitment,
                           _ann_path=locations.annotation_path(project, "film.mp4", "movie"))
    ShotlistVisualizer._persist_best_frame(fake, {"shot_id": content["shot_id"], "best_frame": {"frame": 15, "source": "user"}})
    assert fake._annotation_commitment["revision"] == 2
    assert json.loads(fake._ann_path.read_text())[0]["shot"]["best_frame"]["frame"] == 15


@pytest.mark.parametrize("layout", ["legacy-v1", "media-first-v1"])
def test_registered_collection_conflict_and_explicit_layout(writable, layout):
    from data.annotate import write_annotation_aggregate
    project, registry, instance, content, image = writable
    media = locations.find_media(project, "movie", filename="film.mp4")
    entries = [{"shot": {"shot_id": content["shot_id"], "annotation": {"objects": ["field"]}}}]
    target = locations.annotation_path(project, "film.mp4", "movie", layout=layout)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(entries))
    optional = locations.annotation_path(project, "film.mp4", "movie", ".log", layout=layout)
    optional.write_text("retained optional log")
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", layout)
    other_layout = "legacy-v1" if layout == "media-first-v1" else "media-first-v1"
    other = locations.annotation_path(project, "film.mp4", "movie", layout=other_layout)
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("not the committed collection")
    entries[0]["shot"]["annotation"]["description"] = "first writer"
    arguments = dict(registry=registry, approved=True, expected_revision=1, expected_hashes=commitment["artifacts"])
    assert write_annotation_aggregate(project, "film.mp4", "movie", entries, **arguments) == target
    assert optional.read_text() == "retained optional log"
    assert other.read_text() == "not the committed collection"
    entries[0]["shot"]["annotation"]["description"] = "stale writer"
    with pytest.raises(ValueError, match="conflict"):
        write_annotation_aggregate(project, "film.mp4", "movie", entries, **arguments)
    assert json.loads(target.read_text())[0]["shot"]["annotation"]["description"] == "first writer"


@pytest.mark.parametrize("fault", ["hash", "missing", "expected"])
def test_collection_rejects_source_drift(writable, fault):
    from data.annotate import write_annotation_aggregate
    project, registry, instance, content, image = writable
    media = locations.find_media(project, "movie", filename="film.mp4")
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    path = locations.annotation_path(project, "film.mp4", "movie")
    entries = json.loads(path.read_text())
    if fault == "hash":
        path.write_text(path.read_text() + "\n")
    elif fault == "missing":
        path.unlink()
    else:
        commitment["artifacts"][".annotations.json"] = "0" * 64
    with pytest.raises((ValueError, FileNotFoundError)):
        write_annotation_aggregate(project, "film.mp4", "movie", entries, registry=registry, approved=True,
                                   expected_revision=1, expected_hashes=commitment["artifacts"])
    assert locations.annotation_commitment(project, "film.mp4", "movie", registry)["revision"] == 1


@pytest.mark.parametrize("fault", ["shape", "dtype", "mask", "partial"])
def test_frame_contract_rejection(writable, fault):
    import io
    import numpy as np
    from services.frame_embeddings import commit_frame_artifacts
    from services.resource_publication import publish_annotation_collection
    project, registry, instance, content, image = writable
    media = locations.find_media(project, "movie", filename="film.mp4")
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    arguments = dict(approved=True, expected_revision=1, expected_hashes=commitment["artifacts"])
    if fault == "partial":
        buffer = io.BytesIO()
        np.save(buffer, np.ones((1, 3), dtype="float32"))
        with pytest.raises(ValueError, match="pair"):
            publish_annotation_collection(project, registry, "film.mp4", "movie", {".frames.npy": buffer.getvalue()}, **arguments)
    else:
        vectors = np.ones((2 if fault == "shape" else 1, 3), dtype="float64" if fault == "dtype" else "float32")
        mask = np.ones(1, dtype="int64" if fault == "mask" else "bool")
        with pytest.raises(ValueError):
            commit_frame_artifacts(project, "film.mp4", "movie", vectors, mask, registry=registry, **arguments)
    assert locations.annotation_commitment(project, "film.mp4", "movie", registry)["revision"] == 1


def test_source_edit_invalidates_frame_readback(writable):
    import numpy as np
    from data.annotate import write_annotation_aggregate
    from services.frame_embeddings import commit_frame_artifacts
    project, registry, instance, content, image = writable
    media = locations.find_media(project, "movie", filename="film.mp4")
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "movie", "legacy-v1")
    commit_frame_artifacts(project, "film.mp4", "movie", np.ones((1, 3), dtype="float32"), np.ones(1, dtype=bool),
                           registry=registry, approved=True, expected_revision=1, expected_hashes=commitment["artifacts"])
    commitment = locations.annotation_commitment(project, "film.mp4", "movie", registry)
    token = locations.annotation_context_token(project, registry)
    frame_path = locations.annotation_path(project, "film.mp4", "movie", ".frames.npy", registry=registry)
    before = frame_path.read_bytes()
    entries = [{"shot": {"shot_id": content["shot_id"], "annotation": {"objects": ["field"], "description": "edited"}}}]
    write_annotation_aggregate(project, "film.mp4", "movie", entries, registry=registry, approved=True,
                               expected_revision=2, expected_hashes=commitment["artifacts"])
    assert token != locations.annotation_context_token(project, registry)
    assert frame_path.read_bytes() == before
    with pytest.raises(ValueError, match="Stale"):
        locations.resolve_annotation(project, "film.mp4", "movie", ".frames.npy", registry=registry)


@pytest.mark.parametrize("layout", ["legacy-v1", "media-first-v1"])
def test_gameplay_collection_without_instance_and_optional_members(writable, layout):
    import io
    import numpy as np
    from services.resource_publication import publish_annotation_collection
    project, registry, instance, content, image = writable
    media_id = "game_rdr2_ce5e0bba"
    metadata = project / "data/metadata/gameplay.json"
    metadata.write_text(json.dumps({"media": [{"filename": "recording.mp4", "media_id": media_id}]}))
    media = locations.find_media(project, "gameplay", filename="recording.mp4")
    path = locations.annotation_path(project, "recording.mp4", "gameplay", layout=layout)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"shot": {"shot_id": media_id + "@f000010-f000020", "annotation": {"objects": ["field"]}}}]))
    commitment = ResourceStore(project, registry, approved=True).commit_annotation_layout(media, "gameplay", layout)
    buffer = io.BytesIO()
    np.save(buffer, np.ones((1, 2), dtype="float32"))
    changes = {".annotations.npy": buffer.getvalue(), ".annotations.txt": b"field\n", ".log": b"explicit optional log\n"}
    result = publish_annotation_collection(project, registry, "recording.mp4", "gameplay", changes, approved=True,
                                          expected_revision=1, expected_hashes=commitment["artifacts"])
    assert result["revision"] == 2
    assert locations.resolve_annotation(project, "recording.mp4", "gameplay", ".log", registry=registry).read_bytes() == changes[".log"]
    assert locations.annotation_commitment(project, "recording.mp4", "gameplay", registry)["layout"] == layout
    with sqlite3.connect(registry) as database:
        assert database.execute("SELECT COUNT(*) FROM instances WHERE media_id=?", (media_id,)).fetchone()[0] == 0


def test_registered_descriptive_metadata_and_association_refusal(writable):
    from data.metadata import upsert_json_record
    project, registry, instance, content, image = writable
    upsert_json_record(project, {"media_id": "tmdb_1", "filename": "film.mp4", "title": "New title"}, "movie",
                       registry=registry, approved=True)
    path = project / "data/metadata/movie.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="association"):
        upsert_json_record(project, {"media_id": "tmdb_1", "filename": "renamed.mp4"}, "movie", registry=registry, approved=True)
    assert path.read_bytes() == before


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


def test_registered_index_resolves_only_committed_publications_and_reports_drift(writable, monkeypatch):
    from services.illustration_index import rebuild_index, query_page
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    assert rebuild_index(project, "silhouettes", "movie", registry=registry)["count"] == 1
    before = registry.read_bytes()
    connections = []
    connect = sqlite3.connect
    def track_connection(database, *args, **kwargs):
        connections.append((str(database), kwargs))
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", track_connection)
    page = query_page(project, "silhouettes", "movie")
    assert connections and all(path.endswith("?mode=ro") and kwargs.get("uri") for path, kwargs in connections)
    assert "image_bytes" not in page["records"][0]
    assert page["records"][0]["instance_id"] == instance
    assert page["records"][0]["output_png"] == resolve_silhouette(project, instance, registry=registry)["image"]
    assert registry.read_bytes() == before
    source = resolve_silhouette(project, instance, registry=registry)
    source["image"].unlink()
    stale = query_page(project, "silhouettes", "movie")
    assert stale["status"] == "error" and stale["records"] == []


def test_registered_candidate_summary_uses_current_image_and_stable_identity(writable):
    from services.illustration_index import rebuild_index
    from services.silhouette_discovery import list_active_candidate_references
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    rebuild_index(project, "silhouettes", "movie", registry=registry)
    before = registry.read_bytes()
    page = list_active_candidate_references(project)
    reference = page["entries"][0]
    assert reference["instance_id"] == instance
    assert reference["silhouette_path"].endswith("/silhouette.png")
    assert registry.read_bytes() == before


def test_dirty_assessment_survives_classification_edit_without_reallocation(writable):
    from services.resource_publication import mark_media_resources_dirty
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    original = resolve_silhouette(project, instance, registry=registry)
    before_image = original["image"].read_bytes()
    annotation = locations.annotation_path(project, "film.mp4", "movie")
    annotation.write_text(json.dumps([{"shot": {"shot_id": content["shot_id"], "annotation": {"objects": ["different"]}}}]))
    result = mark_media_resources_dirty(project, "film.mp4", "movie", "annotation_classification_edit", registry=registry, approved=True)
    assert result["updated"] == 1
    current = resolve_silhouette(project, instance, registry=registry)
    assert current["record"]["instance_id"] == instance and current["image"].read_bytes() == before_image
    assert current["record"]["source_validation"]["dirty"] is True
    assert current["record"].get("assignment", {}).get("state", "active") == "active"


def test_shotlist_commit_hook_marks_registered_resources_dirty(writable):
    from data.shotlist import write_shotlist
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    rows = [{"start_frame": 10, "end_frame": 20, "shot_id": content["shot_id"]}]
    write_shotlist(project, "film.mp4", "movie", rows)
    rows[0]["end_frame"] = 19
    write_shotlist(project, "film.mp4", "movie", rows, registry=registry, approved=True)
    current = resolve_silhouette(project, instance, registry=registry)
    assert current["record"]["source_validation"]["reasons"] == ["committed_shotlist_edit"]
    assert current["record"]["instance_id"] == instance


def test_dirty_assessment_cannot_change_classification_or_create_resource(writable):
    from services.resource_publication import mark_media_resources_dirty
    project, registry, instance, content, image = writable
    assert mark_media_resources_dirty(project, "film.mp4", "movie", "edit")["activated"] is False
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    assessment = {"dirty": True, "reasons": ["edit"], "instance_id": instance,
                  "registered_shot_id": content["shot_id"], "current_shot_id": content["shot_id"]}
    with pytest.raises(ValueError, match="cannot change"):
        publish_resource(project, registry, instance, "silhouettes", {**content, "label": "different", "source_validation": assessment}, image,
                         expected_revision=1, approved=True, _assessment_only=True)


def test_cli_explicit_index_and_committed_search_use_shared_services(writable, monkeypatch, capsys):
    import cli
    from services.search import SearchCorpus, search_shots
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    record = locations.find_media(project, "movie", media_id="tmdb_1")
    legacy = locations.annotation_path(project, "film.mp4", "movie")
    canonical = locations.annotation_path(project, "film.mp4", "movie", layout="media-first-v1")
    canonical.parent.mkdir(parents=True, exist_ok=True)
    legacy.rename(canonical)
    ResourceStore(project, registry, approved=True).commit_annotation_layout(record, "movie", "media-first-v1")
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: str(project) if key == "path" else default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: pytest.fail("CLI read changed preferences"))
    before = registry.read_bytes(), canonical.read_bytes()
    parser = cli.build_parser()
    args = parser.parse_args(["index", "illustration", "--resource-registry", str(registry)])
    args.func(args)
    assert "Silhouettes: 1 indexed" in capsys.readouterr().out
    args = parser.parse_args(["search", "field", "--field", "objects", "--all", "--resource-registry", str(registry)])
    args.func(args)
    assert json.loads(capsys.readouterr().out)["results"][0]["shot_id"] == content["shot_id"]
    assert before == (registry.read_bytes(), canonical.read_bytes())
    with pytest.raises(ValueError, match="registry context"):
        search_shots("field", None, "objects", None, None, True, project_path=str(project), corpus=SearchCorpus(str(project)), registry=registry)
    canonical.write_text("[]")
    with pytest.raises(ValueError):
        search_shots("field", None, "objects", None, None, True, project_path=str(project), registry=registry)


def test_mcp_registered_candidates_use_service_and_report_stale_commitment(writable, monkeypatch):
    import asyncio
    from mcp_server import mcp_server as server
    from services.illustration_index import rebuild_index
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    rebuild_index(project, "silhouettes", "movie", registry=registry)
    monkeypatch.setattr(server, "_ctx", lambda: (str(project), "movie"))
    before = registry.read_bytes()
    response = json.loads(server.list_silhouette_candidates("field"))
    assert response["entries"][0]["instance_id"] == instance
    assert registry.read_bytes() == before
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert "list_silhouette_candidates" in names
    assert not names.intersection({"adopt_registry", "repair_resource", "publish_resource"})
    resolve_silhouette(project, instance, registry=registry)["image"].unlink()
    response = json.loads(server.list_silhouette_candidates("field"))
    assert not response.get("entries") and "STALE_RESOURCE" in json.dumps(response)
    assert registry.read_bytes() == before


def test_registered_both_modes_navigation_and_dirty_propagation(writable):
    from services.illustration_index import rebuild_all, query_page
    from services.resource_publication import mark_media_resources_dirty
    from visualizers.components.illustration_source import EngravingSource
    project, registry, instance, silhouette, image = writable
    frame = _engraving_draft(writable)
    publish_resource(project, registry, instance, "engravings", frame, image, mode="frame", approved=True)
    generation = {**frame["generation"], "service": "local", "seed": 42, "model_version": "v1"}
    isolated = {**frame, "mode": "isolated", "generation": generation}
    run = publish_generation_run(project, registry, instance, "objects", "field", "local", 1,
                                 {"source_instance_id": instance, "mode": "isolated", "generation": generation, "source": frame["source"]},
                                 {"input.png": image}, approved=True)
    isolated["published_run"] = {"provider": "local", "number": 1, "sha256": digest(run["run"])}
    publish_resource(project, registry, instance, "engravings", isolated, image, mode="isolated", approved=True)
    assert rebuild_all(project, "movie", registry=registry)["engravings"]["count"] == 2
    origin = query_page(project, "silhouettes", "movie")["records"][0]
    source = EngravingSource(str(project))
    destination = source.resolve_destination({"origin": origin}, 50)
    assert {choice["mode"] for choice in destination["choices"]} == {"frame", "isolated"}
    before = {mode: resolve_engraving(project, instance, registry=registry, mode=mode)["image"].read_bytes() for mode in ("frame", "isolated")}
    assert mark_media_resources_dirty(project, "film.mp4", "movie", "source_frame_edit", registry=registry, approved=True)["updated"] == 3
    for mode in before:
        current = resolve_engraving(project, instance, registry=registry, mode=mode)
        assert current["record"]["source_validation"]["reasons"] == ["source_frame_edit"]
        assert current["record"]["instance_id"] == instance and current["image"].read_bytes() == before[mode]


@pytest.mark.parametrize("stage", ["prepared", "image-installed", "metadata-installed", "before-commit", "committed"])
def test_dirty_assessment_recovery_preserves_invalidated_source_and_image(writable, stage):
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    current = resolve_silhouette(project, instance, registry=registry)
    original = current["image"].read_bytes()
    annotation = locations.annotation_path(project, "film.mp4", "movie")
    annotation.write_text("[]")
    metadata = {**current["record"], "source_validation": {"dirty": True, "reasons": ["annotation_classification_edit"],
                "instance_id": instance, "registered_shot_id": content["shot_id"], "current_shot_id": content["shot_id"]}}
    def interrupt(current_stage):
        if current_stage == stage:
            raise RuntimeError("interrupted")
    with pytest.raises(RuntimeError, match="interrupted"):
        publish_resource(project, registry, instance, "silhouettes", metadata, current["image"], expected_revision=1,
                         approved=True, _assessment_only=True, interrupt=interrupt)
    operation = pending_id(registry)
    assert recover_publication(project, registry, operation, approved=True)["state"] == "finished"
    assert recover_publication(project, registry, operation, approved=True)["state"] == "finished"
    recovered = resolve_silhouette(project, instance, registry=registry)
    assert recovered["record"]["source_validation"]["reasons"] == ["annotation_classification_edit"]
    assert recovered["record"]["instance_id"] == instance and recovered["image"].read_bytes() == original
    assert annotation.read_text() == "[]"


def test_registered_consumers_cache_revision_and_paired_arrays(writable):
    import numpy as np
    from services import corpus_stats, vocabulary_index, sync_frame_match, scene_detection
    from generators.cloud import extract_annotation_words
    project, registry, instance, content, image = writable
    record = locations.find_media(project, "movie", media_id="tmdb_1")
    legacy = locations.annotation_path(project, "film.mp4", "movie")
    canonical = locations.annotation_path(project, "film.mp4", "movie", layout="media-first-v1")
    canonical.parent.mkdir(parents=True, exist_ok=True)
    legacy.rename(canonical)
    for suffix, array in ((".frames.npy", np.ones((1, 2))), (".frames.valid.npy", np.array([False]))):
        np.save(locations.annotation_path(project, "film.mp4", "movie", suffix, layout="media-first-v1"), array)
    store = ResourceStore(project, registry, approved=True)
    store.commit_annotation_layout(record, "movie", "media-first-v1")
    assert corpus_stats._aggregate_annotated_shots(project, registry=registry)["total"] == 1
    assert corpus_stats._count_frame_embeddings(project, registry=registry) == {"movie": 1}
    first = corpus_stats._annotations_fingerprint(project, registry=registry)
    source_hash = vocabulary_index._compute_source_hash(project, "movie", registry=registry)
    catalog = sync_frame_match.load_frame_catalog(project, "movie", all_items=True, registry=registry)
    assert catalog.valid_mask.tolist() == [False] and catalog.vectors.shape == (1, 2)
    assert extract_annotation_words(project, field="objects", registry=registry)["field"] == 1
    assert scene_detection._load_annotations(project, "film.mp4", "movie", registry=registry)[0]["shot"]["shot_id"] == content["shot_id"]
    from services.resource_publication import publish_annotation_collection
    commitment = locations.annotation_commitment(project, "film.mp4", "movie", registry)
    publish_annotation_collection(project, registry, "film.mp4", "movie", {".log": b"controlled cache revision"},
                                  expected_revision=1, expected_hashes=commitment["artifacts"], approved=True)
    assert corpus_stats._annotations_fingerprint(project, registry=registry) != first
    assert vocabulary_index._compute_source_hash(project, "movie", registry=registry) != source_hash
    assert sync_frame_match.load_frame_catalog(project, "movie", all_items=True, registry=registry) is not catalog
    canonical.write_text("[]")
    for call in (lambda: vocabulary_index._compute_source_hash(project, "movie", registry=registry),
                 lambda: sync_frame_match.load_frame_catalog(project, "movie", all_items=True, registry=registry),
                 lambda: extract_annotation_words(project, registry=registry)):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("stage", ["dependent-completed", "image-installed"])
def test_multi_resource_dirty_resume_tracks_children_and_is_idempotent(writable, stage):
    from services.resource_publication import mark_media_resources_dirty, resume_dirty_propagation
    project, registry, instance, content, image = writable
    engraving = _engraving_draft(writable)
    publish_resource(project, registry, instance, "engravings", engraving, image, mode="frame", approved=True)
    original = image.read_bytes()
    def interrupt(current_stage):
        if current_stage == stage:
            raise RuntimeError("interrupted propagation")
    with pytest.raises(RuntimeError):
        mark_media_resources_dirty(project, "film.mp4", "movie", "committed_edit", registry=registry, approved=True,
                                   edit_id="fixture-edit", interrupt=interrupt)
    with sqlite3.connect(registry) as database:
        parent = database.execute("SELECT state,details FROM publication_operations WHERE operation_id='fixture-edit'").fetchone()
        assert parent[0] == "prepared"
        if stage == "dependent-completed":
            assert len(json.loads(parent[1])["completed"]) == 1
    assert resume_dirty_propagation(project, registry, "fixture-edit", approved=True)["updated"] == 2
    revisions = [resolve_silhouette(project, instance, registry=registry)["revision"], resolve_engraving(project, instance, registry=registry)["revision"]]
    assert resume_dirty_propagation(project, registry, "fixture-edit", approved=True)["state"] == "finished"
    assert revisions == [resolve_silhouette(project, instance, registry=registry)["revision"], resolve_engraving(project, instance, registry=registry)["revision"]]
    assert resolve_silhouette(project, instance, registry=registry)["image"].read_bytes() == original


def test_registered_lifecycle_plans_exact_pair_and_protects_runs(writable):
    from services.engraving_lifecycle import scan_engravings, plan_removal, apply_removal
    from services.silhouette_catalog import clear_catalog
    from data.annotate import remove_file_annotations
    project, registry, instance, content, image = writable
    engraving = _engraving_draft(writable)
    publish_resource(project, registry, instance, "engravings", engraving, image, mode="frame", approved=True)
    entries = scan_engravings(project, registry=registry)
    assert len(entries) == 1 and entries[0]["instance_id"] == instance
    plan = plan_removal(project, entries)
    current = resolve_engraving(project, instance, registry=registry)
    assert plan["metadata_files"] == [current["metadata"]] and plan["canonical_assets"] == [current["image"]]
    assert not plan["provenance_files"] and not plan_removal(project, entries, include_provenance=True)["safe"]
    before = registry.read_bytes(), current["image"].read_bytes()
    from services.engraving_local_publish import set_review_state, PublicationError
    original_record = current["metadata"].read_bytes()
    with pytest.raises(PublicationError, match="lifecycle commits"):
        set_review_state(current["metadata"], "accepted", project_path=project, registry=registry)
    assert current["metadata"].read_bytes() == original_record
    with pytest.raises(ValueError, match="retirement"):
        apply_removal(project, plan, force=True)
    assert clear_catalog(project, "movie", dry_run=True, registry=registry)["resources"][0]["instance_id"] == instance
    with pytest.raises(ValueError, match="retirement"):
        clear_catalog(project, "movie", registry=registry)
    assert remove_file_annotations(project, "film.mp4", registry=registry, dry_run=True)
    with pytest.raises(ValueError, match="deletion"):
        remove_file_annotations(project, "film.mp4", registry=registry)
    assert before == (registry.read_bytes(), current["image"].read_bytes())


@pytest.fixture
def registered_gameplay(tmp_path):
    from PIL import Image
    media_id = "game_rdr2_ce5e0bba"
    record = {"media_type": "gameplay", "media_id": media_id, "filename": "recording.mp4", "filename_stem": "recording",
              "shot_id": media_id + "@f000010-f000020", "frame": 12, "field": "objects", "label": "wagon"}
    source = tmp_path / "data/silhouettes/catalog/gameplay/recording/wagon/object_0008.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(record))
    Image.new("RGBA", (2, 2), (20, 30, 40, 255)).save(source.with_suffix(".png"))
    proposal = InstanceRegistry(tmp_path, tmp_path / RESEARCH / "proposal.sqlite3")
    instance = proposal.register([prepare_registration({**record, "path": source}, tmp_path)])[0][1]
    proposal_path = proposal.path
    proposal.close()
    metadata = tmp_path / "data/metadata/gameplay.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"media": [record]}))
    registry = tmp_path / "data/indexes/resources.sqlite3"
    adopt_registry(tmp_path, proposal_path, registry, exceptions=[], approved=True)
    annotation = locations.annotation_path(tmp_path, "recording.mp4", "gameplay")
    annotation.parent.mkdir(parents=True)
    annotation.write_text(json.dumps([{"shot": {"shot_id": record["shot_id"], "annotation": {"objects": ["wagon"]}, "best_frame": {"frame": 12}}}]))
    content = {**record, "publication_contract": "media-first-v1", "resource_kind": "silhouettes", "instance_id": instance,
               "bbox": [0, 0, 2, 2], "frame_size": [2, 2], "mask_area": 4, "sam_model": "fixture-sam",
               "frame_match_model": "fixture-clip", "source_frame": "frame:12"}
    publish_resource(tmp_path, registry, instance, "silhouettes", content, source.with_suffix(".png"), approved=True)
    canonical = locations.annotation_path(tmp_path, "recording.mp4", "gameplay", layout="media-first-v1")
    canonical.parent.mkdir(parents=True, exist_ok=True)
    annotation.rename(canonical)
    ResourceStore(tmp_path, registry, approved=True).commit_annotation_layout(record, "gameplay", "media-first-v1")
    return tmp_path, registry, instance, content, canonical


_resource_qt_application = None


@pytest.fixture
def resource_app():
    from PyQt5.QtWidgets import QApplication
    global _resource_qt_application
    _resource_qt_application = QApplication.instance() or QApplication([])
    return _resource_qt_application


def test_registered_gameplay_mcp_and_segmentation_consume_current_annotations(registered_gameplay, monkeypatch, resource_app):
    from mcp_server import mcp_server as server
    from visualizers.segmentation_visualizer import SAMExplorer
    from data.shotlist import write_shotlist
    from tool import prefs
    project, registry, instance, content, annotation = registered_gameplay
    monkeypatch.setattr(server, "_ctx", lambda: (str(project), "gameplay"))
    response = json.loads(server.search_shots("wagon", media_type="gameplay", resource_registry=str(registry)))
    assert response["results"][0]["shot_id"] == content["shot_id"]
    assert resolve_silhouette(project, instance, registry=registry)["record"]["media_type"] == "gameplay"
    store = {"path": str(project)}
    monkeypatch.setattr(prefs, "get", lambda key, default=None, **kwargs: store.get(key, default))
    monkeypatch.setattr(prefs, "set", lambda key, value, *args, **kwargs: store.__setitem__(key, value))
    write_shotlist(project, "recording.mp4", "gameplay", [{"shot_id": content["shot_id"], "start_frame": 10, "end_frame": 20, "Scene": "1"}])
    window = SAMExplorer(str(project), "gameplay", registry=registry)
    try:
        window._films = [locations.find_media(project, "gameplay", filename="recording.mp4")]
        window._film_idx = 0
        window._filename = "recording.mp4"
        window._load_shots()
        assert window._shots[0]["best_frame"]["frame"] == 12
        annotation.unlink()
        window._load_shots()
        assert "committed annotations" in window._status_lbl.text()
    finally:
        window.close()
        window.deleteLater()
        from PyQt5.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        resource_app.processEvents()


def test_real_book_insertion_resolves_registered_bytes_at_copy_boundary(writable, monkeypatch, resource_app):
    import fitz
    from visualizers.book_visualizer import BookVisualizerWindow
    from services.illustration_index import rebuild_index, query_page
    from data.book import book_dir
    from tool import prefs
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    rebuild_index(project, "silhouettes", "movie", registry=registry)
    record = query_page(project, "silhouettes", "movie")["records"][0]
    store = {"path": str(project)}
    monkeypatch.setattr(prefs, "get", lambda key, default=None, **kwargs: store.get(key, default))
    monkeypatch.setattr(prefs, "set", lambda key, value, *args, **kwargs: store.__setitem__(key, value))
    window = BookVisualizerWindow(str(project))
    window._doc = fitz.open()
    window._doc.new_page()
    window._slug = "registered-book"
    monkeypatch.setattr(window, "_on_layer_committed", lambda layer: None)
    monkeypatch.setattr(window._overlay, "_visible_page_rects", lambda: {})
    try:
        window._insert_silhouette_at(str(project / "stale-selection.png"), record, 0, 0.5, 0.5)
        layer = window._overlay.current_layers()[-1]
        copied = book_dir(project, window._slug) / layer["source"]
        assert not copied.is_relative_to(project / "data") and copied.read_bytes() == image.read_bytes()
        assert not __import__("pathlib").Path(layer["source"]).is_absolute()
        resolve_silhouette(project, instance, registry=registry)["image"].unlink()
        count = len(window._overlay.current_layers())
        with pytest.raises((OSError, ValueError)):
            window._insert_silhouette_at(str(image), record, 0, 0.5, 0.5)
        assert len(window._overlay.current_layers()) == count and copied.exists()
    finally:
        window.close()
        window.deleteLater()
        from PyQt5.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        resource_app.processEvents()


def test_registered_metadata_association_write_is_explicitly_deferred(writable):
    from data.metadata import save_json_metadata
    project, registry, instance, content, image = writable
    path = project / "data/metadata/movie.json"
    before = path.read_bytes(), registry.read_bytes()
    with pytest.raises(ValueError, match="association edits"):
        save_json_metadata(project, "movie", [{"media_id": "tmdb_1", "filename": "renamed.mp4"}], registry=registry, approved=True)
    assert before == (path.read_bytes(), registry.read_bytes())


def test_shotlist_retry_resumes_pending_postcommit_propagation(writable, monkeypatch):
    from data.shotlist import write_shotlist, read_shotlist
    from services import resource_publication
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    rows = [{"shot_id": content["shot_id"], "start_frame": 10, "end_frame": 20}]
    write_shotlist(project, "film.mp4", "movie", rows)
    resume = resource_publication.resume_dirty_propagation
    monkeypatch.setattr(resource_publication, "resume_dirty_propagation", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("after CSV commit")))
    rows[0]["end_frame"] = 19
    with pytest.raises(RuntimeError, match="CSV commit"):
        write_shotlist(project, "film.mp4", "movie", rows, registry=registry, approved=True)
    assert read_shotlist(project, "film.mp4", "movie")[0]["end_frame"] == "19"
    monkeypatch.setattr(resource_publication, "resume_dirty_propagation", resume)
    with sqlite3.connect(registry) as database:
        operation = database.execute("SELECT operation_id FROM publication_operations WHERE kind='dirty-propagation' AND state!='finished'").fetchone()[0]
    assert recover_publication(project, registry, operation, approved=True)["state"] == "finished"
    write_shotlist(project, "film.mp4", "movie", rows, registry=registry, approved=True)
    assert resolve_silhouette(project, instance, registry=registry)["record"]["source_validation"]["dirty"] is True
    with sqlite3.connect(registry) as database:
        assert database.execute("SELECT count(*) FROM publication_operations WHERE kind='dirty-propagation' AND state!='finished'").fetchone()[0] == 0


def test_actual_shot_save_handler_uses_opt_in_dirty_hook(writable):
    from types import SimpleNamespace
    from visualizers.shot_visualizer import ShotlistVisualizer
    from data.shotlist import write_shotlist
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    rows = [{"shot_id": content["shot_id"], "start_frame": 10, "end_frame": 20}]
    write_shotlist(project, "film.mp4", "movie", rows)
    rows[0]["end_frame"] = 18
    handler = SimpleNamespace(project_path=project, filename="film.mp4", media_type="movie", shots=rows,
                              _resource_registry=registry, _resource_edit_approved=True, modified=True,
                              save_button=SimpleNamespace(setEnabled=lambda enabled: None))
    ShotlistVisualizer.save_changes(handler)
    assert handler.modified is False
    assert resolve_silhouette(project, instance, registry=registry)["record"]["source_validation"]["reasons"] == ["committed_shotlist_edit"]


def test_registered_live_preprocessing_ignores_stale_selected_png(writable):
    from services.engraving_preprocess import preprocess_engraving_source
    from services.illustration_index import rebuild_index, query_page
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    rebuild_index(project, "silhouettes", "movie", registry=registry)
    record = query_page(project, "silhouettes", "movie")["records"][0]
    arguments = dict(source_png=project / "stale.png", engraving_id="fixture", parent_layer_id="layer", source_silhouette_id=instance,
                     page_idx=0, x=0.5, y=0.5, width=0.1, height=0.1, rotation=0, flip_h=False, flip_v=False,
                     page_pt_w=72, page_pt_h=72, cache_dir=project / "outputs/agent/preprocess", project_path=project, resource_record=record)
    result = preprocess_engraving_source(**arguments)
    assert __import__("pathlib").Path(result["preprocessing_path"]).is_file()
    resolve_silhouette(project, instance, registry=registry)["image"].unlink()
    with pytest.raises((OSError, ValueError)):
        preprocess_engraving_source(**arguments)


def test_actual_cli_registered_vocabulary_build_and_stale_revision(registered_gameplay, monkeypatch):
    import cli
    from services.vocabulary_index import load_vocabulary_index
    from services.derived_vocabulary import load_derived_vocabulary
    project, registry, instance, content, annotation = registered_gameplay
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: str(project) if key == "path" else default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: None)
    monkeypatch.setattr("data.index.load_vocabulary_fields", lambda project: ["objects"])
    monkeypatch.setattr("data.index.load_atomic_fields", lambda project: ["objects"])
    arguments = cli.build_parser().parse_args(["index", "vocabulary", "--media", "gameplay", "--resource-registry", str(registry), "--force"])
    arguments.func(arguments)
    assert load_vocabulary_index(project, "gameplay", registry=registry)["meta"]["files_processed"] == 1
    assert load_derived_vocabulary(project, "gameplay", registry=registry)["meta"]["files_processed"] == 1
    from services.resource_publication import publish_annotation_collection
    filename = locations.find_media(project, "gameplay", media_id=content["media_id"])["filename"]
    commitment = locations.annotation_commitment(project, filename, "gameplay", registry)
    publish_annotation_collection(project, registry, filename, "gameplay", {".log": b"controlled cache revision"},
                                  expected_revision=1, expected_hashes=commitment["artifacts"], approved=True)
    for loader in (load_vocabulary_index, load_derived_vocabulary):
        with pytest.raises(ValueError):
            loader(project, "gameplay", registry=registry)


def test_registered_history_is_indexed_but_not_an_active_source(writable):
    from services.resource_resolution import indexed_publications
    from services.illustration_index import rebuild_index, query_page
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    current = resolve_silhouette(project, instance, registry=registry)
    metadata = {**current["record"], "assignment": {"state": "inactive"}}
    from data.annotate import atomic_write_text
    atomic_write_text(current["metadata"], json.dumps(metadata))
    store = ResourceStore(project, registry, approved=True)
    binding = locations.registered_instance(project, registry, instance)
    from data.resource_storage import relative_path
    details = {"metadata": metadata, "record_path": relative_path(project, current["metadata"]), "image_path": relative_path(project, current["image"]),
               "record_sha256": digest(current["metadata"]), "image_sha256": digest(current["image"]), "expected_revision": 1}
    with store.transaction() as database:
        database.execute("INSERT INTO publication_operations VALUES(?,?,?,?,?,?)", ("history-fixture", instance, "silhouettes", "", "files_published", json.dumps(details)))
        store.commit_location(database, binding, "silhouettes", "", current, metadata, 1, operation_id="history-fixture")
        database.execute("UPDATE publication_operations SET state='finished' WHERE operation_id='history-fixture'")
    assert len(indexed_publications(project, "silhouettes", "movie", registry=registry)) == 1
    rebuild_index(project, "silhouettes", "movie", registry=registry)
    assert query_page(project, "silhouettes", "movie")["records"] == []
    assert len(query_page(project, "silhouettes", "movie", assignment_state="inactive")["records"]) == 1
    with pytest.raises(ValueError):
        resolve_silhouette(project, instance, registry=registry)


def test_registered_shot_navigation_context_is_preserved(writable, monkeypatch):
    from visualizers import shot_visualizer
    project, registry, instance, content, image = writable
    assert shot_visualizer._ipc_socket_path(str(project)) != shot_visualizer._ipc_socket_path(str(project), registry=registry)
    delivered = []
    monkeypatch.setattr(shot_visualizer, "ipc_send_load", lambda *args, **kwargs: delivered.append(kwargs) or True)
    shot_visualizer.open_at_shot(str(project), "film.mp4", registry=registry)
    assert delivered[0]["registry"] == registry


def test_existing_registry_lock_supports_nested_recovery_without_upgrade(writable):
    from data.resource_storage import registry_lock
    project, registry, instance, content, image = writable
    with registry_lock(project, registry, exclusive=True):
        with registry_lock(project, registry):
            with ResourceStore(project, registry, approved=True).transaction() as database:
                assert database.execute("SELECT count(*) FROM instances").fetchone()[0] == 1
    with registry_lock(project, registry):
        with pytest.raises(ValueError, match="upgraded"):
            with registry_lock(project, registry, exclusive=True):
                pytest.fail("Read lock was upgraded")


def test_registered_mosaic_worker_context_and_close_invalidates_late_result(registered_gameplay, resource_app, monkeypatch):
    import threading
    from PyQt5.QtCore import QCoreApplication, QEvent
    from visualizers.mosaic_visualizer import MosaicVisualizer
    from services import search
    project, registry, instance, content, annotation = registered_gameplay
    monkeypatch.setattr("tool.prefs.get", lambda key, default=None: str(project) if key == "path" else default)
    monkeypatch.setattr("tool.prefs.set", lambda *args, **kwargs: None)
    entered, release = threading.Event(), threading.Event()
    captured = []
    original = search.vocabulary_from_index
    def controlled(*args, **kwargs):
        captured.append(kwargs.get("registry"))
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(search, "vocabulary_from_index", controlled)
    window = MosaicVisualizer(str(project), media_type="gameplay", registry=registry)
    worker = None
    try:
        window.field_combo.setCurrentIndex(0)
        window._start_vocabulary_load()
        worker = window._vocab_worker
        resource_app.processEvents()
        assert entered.wait(1)
        assert captured == [registry]
        previous = window._vocab_request_id
        window.close()
        assert window._vocab_request_id > previous
        release.set()
        assert worker.wait(3000)
        resource_app.processEvents()
        assert window._vocab_request_id > previous
    finally:
        release.set()
        if worker is not None:
            try:
                worker.wait(3000)
            except RuntimeError:
                pass
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_committed_association_changes_preserve_instance_and_unknown_evidence(writable):
    from data.shotlist import write_shotlist
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    original_bytes = resolve_silhouette(project, instance, registry=registry)["image_bytes"]
    write_shotlist(project, "film.mp4", "movie", [{"shot_id": content["shot_id"], "start_frame": 10, "end_frame": 20}])
    current_shot = "tmdb_1@f000010-f000019"
    write_shotlist(project, "film.mp4", "movie", [{"shot_id": current_shot, "start_frame": 10, "end_frame": 19}], registry=registry, approved=True)
    current = resolve_silhouette(project, instance, registry=registry)
    assert current["record"]["instance_id"] == instance
    assert current["record"]["source_validation"]["current_shot_id"] == current_shot
    assert current["record"]["source_validation"]["current_association_known"] is True
    write_shotlist(project, "film.mp4", "movie", [], registry=registry, approved=True)
    current = resolve_silhouette(project, instance, registry=registry)
    assert current["image_bytes"] == original_bytes
    assert current["record"]["source_validation"]["current_association_known"] is False
    assert current["record"]["source_validation"]["current_shot_id"] == current_shot
    assert len(current["record"]["source_validation"]["committed_edits"]) == 2


def test_precommit_intent_cannot_mark_resources_before_expected_commit(writable):
    from services.resource_publication import mark_media_resources_dirty
    project, registry, instance, content, image = writable
    publish_resource(project, registry, instance, "silhouettes", content, image, approved=True)
    source = locations.annotation_path(project, "film.mp4", "movie")
    before = source.read_bytes()
    intent = mark_media_resources_dirty(project, "film.mp4", "movie", "committed_annotation_edit", registry=registry, approved=True,
                                      committed_path=source, committed_sha256=__import__("hashlib").sha256(b"[]").hexdigest(), _defer=True)
    with pytest.raises(ValueError, match="not committed"):
        recover_publication(project, registry, intent["operation_id"], approved=True)
    current = resolve_silhouette(project, instance, registry=registry)
    assert not current["record"].get("source_validation", {}).get("dirty")
    assert current["revision"] == 1 and source.read_bytes() == before


def test_actual_registered_cli_retirement_plans_do_not_change_assets(registered_gameplay, monkeypatch):
    import cli
    project, registry, instance, content, annotation = registered_gameplay
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: str(project) if key == "path" else default)
    monkeypatch.setattr(cli.prefs, "set", lambda *args, **kwargs: None)
    before = registry.read_bytes(), annotation.read_bytes(), resolve_silhouette(project, instance, registry=registry)["image_bytes"]
    arguments = cli.build_parser().parse_args(["index", "silhouette", "clear", "--all", "--media", "gameplay", "--resource-registry", str(registry)])
    arguments.func(arguments)
    assert before == (registry.read_bytes(), annotation.read_bytes(), resolve_silhouette(project, instance, registry=registry)["image_bytes"])
    arguments = cli.build_parser().parse_args(["annotate", "remove", "--all", "--media", "gameplay", "--resource-registry", str(registry)])
    arguments.func(arguments)
    assert before == (registry.read_bytes(), annotation.read_bytes(), resolve_silhouette(project, instance, registry=registry)["image_bytes"])


def test_optional_annotation_reader_still_rejects_missing_commitment(registered_gameplay):
    project, registry, instance, content, annotation = registered_gameplay
    metadata = project / "data/metadata/gameplay.json"
    document = json.loads(metadata.read_text())
    document["media"].append({"filename": "unannotated.mp4", "media_id": "game_rdr2_abcd0000"})
    from data.annotate import atomic_write_text
    atomic_write_text(metadata, json.dumps(document))
    missing = locations.resolve_annotation(project, "unannotated.mp4", "gameplay", registry=registry, required=False)
    assert not missing.exists()
    annotation.unlink()
    with pytest.raises((ValueError, OSError)):
        locations.resolve_annotation(project, content["filename"], "gameplay", registry=registry, required=False)


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