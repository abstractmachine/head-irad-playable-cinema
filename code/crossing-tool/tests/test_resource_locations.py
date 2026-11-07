import json
import sqlite3

import pytest

from data import resource_locations as locations
from data.media_id import build_shot_id, instance_identifier
from services import resource_resolution as resolution
from services.silhouette_canonical_identity import InstanceRegistry, RESEARCH, prepare_registration


@pytest.fixture
def registered(tmp_path):
    record = {"media_id": "tmdb_61044", "filename": "Companeros (1970) {tmdb-61044}.mp4"}
    metadata = {**record, "media_type": "movie", "filename_stem": record["filename"][:-4],
                "shot_id": "tmdb_61044@f078383-f078476", "field": "animals", "label": "bird", "frame": 78384}
    path = tmp_path / f"data/silhouettes/catalog/movie/{metadata['filename_stem']}/bird/object_0001.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(metadata))
    path.with_suffix(".png").write_bytes(b"fixture image")
    registry_path = tmp_path / RESEARCH / "registry.sqlite3"
    registry = InstanceRegistry(tmp_path, registry_path)
    instance = registry.register([prepare_registration({**metadata, "path": path}, tmp_path)])[0][1]
    registry.close()
    metadata_file = tmp_path / "data/metadata/movie.json"
    metadata_file.parent.mkdir(parents=True)
    metadata_file.write_text(json.dumps({"media": [record]}))
    return tmp_path, registry_path, instance, record, path, metadata


def install_current_location(fixture, kind="silhouettes", mode="", *, label="bird", field="animals"):
    project, registry, instance, record, source, metadata = fixture
    builder = locations.silhouette_destination if kind == "silhouettes" else locations.engraving_destination
    args = (project, record, "movie", instance, field, label)
    destination = builder(*args) if kind == "silhouettes" else builder(*args, mode)
    destination["dir"].mkdir(parents=True, exist_ok=True)
    content = {**metadata, "instance_id": instance, "source_instance_id": instance,
               "field": field, "label": label, "status": "generated", "mode": mode}
    destination["metadata"].write_text(json.dumps(content))
    destination["image"].write_bytes(b"current fixture image")
    with sqlite3.connect(registry) as database:
        database.execute("INSERT OR REPLACE INTO meta VALUES('location_schema_version','1')")
        database.execute("CREATE TABLE IF NOT EXISTS resource_locations(instance_id TEXT,kind TEXT,mode TEXT,layout TEXT,revision INTEGER,record_path TEXT,image_path TEXT,field TEXT,label TEXT)")
        database.execute("INSERT INTO resource_locations VALUES(?,?,?,?,?,?,?,?,?)",
                         (instance, kind, mode, "media-first-v1", 1,
                          destination["metadata"].relative_to(project).as_posix(),
                          destination["image"].relative_to(project).as_posix(), field, label))
    return destination


@pytest.mark.parametrize("media_type", ["movie", "gameplay"])
def test_approved_destinations_are_pure(tmp_path, media_type):
    media_id = "tmdb_61044" if media_type == "movie" else "game_rdr2_ce5e0bba"
    record = {"media_id": media_id, "filename": "Companeros (1970) {tmdb-61044}.mp4"}
    item = "Companeros (1970) {tmdb-61044}" if media_type == "movie" else media_id
    instance = instance_identifier(build_shot_id(media_id, 78383, 78476), 1)
    annotation = locations.annotation_destination(tmp_path, record, media_type)
    assert annotation == tmp_path / f"data/annotations/shots/{media_type}/{item}/{record['filename'][:-4]}.annotations.json"
    for kind in ("silhouettes", "engravings"):
        path = locations.artifact_directory(tmp_path, kind, record, media_type, instance, "animals", "NOTICES board")
        assert path == tmp_path / f"data/{kind}/{media_type}/{item}/animals/notices-board/{instance}"
        assert "catalog" not in path.parts and "object_0001" not in path.parts
        assert path == locations.artifact_directory(tmp_path, kind, record, media_type, instance, "animals", "notices board")
    for mode in locations.MODES:
        publication = locations.engraving_destination(tmp_path, record, media_type, instance, "animals", "bird", mode)
        assert publication["metadata"].name == f"engraving-{mode}.json"
        assert publication["image"].name == f"engraving-{mode}.png"
    for provider in locations.PROVIDERS:
        run = locations.generation_run_destination(tmp_path, record, media_type, instance, "animals", "bird", provider, 1)
        assert run.parts[-3:] == ("runs", provider, "run-001")
    assert not list(tmp_path.iterdir())


def test_missing_classification_and_temporal_contract(tmp_path):
    record = {"media_id": "tmdb_1", "filename": "film.mp4"}
    instance = "tmdb_1@f000010-f000020#0001"
    with pytest.raises(ValueError):
        locations.silhouette_destination(tmp_path, record, "movie", instance, "", "bird")
    assessment = locations.source_validation(instance, "tmdb_1@f000010-f000030")
    assert assessment["dirty"] and assessment["instance_id"] == instance
    assert not locations.source_validation(instance, "tmdb_1@f000010-f000020")["dirty"]


def test_registered_legacy_lookup_and_readonly_safety(registered, monkeypatch):
    project, registry, instance, record, source, metadata = registered
    before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError("Lookup attempted mutation/discovery")
    monkeypatch.setattr(InstanceRegistry, "register", forbidden)
    monkeypatch.setattr(locations.Path, "mkdir", forbidden)
    from services import illustration_index
    monkeypatch.setattr(illustration_index, "rebuild_index", forbidden)
    result = resolution.resolve_silhouette(project, instance, registry=registry)
    assert result["metadata"] == source and result["instance_id"] == instance
    assert result["layout"] == "legacy-v1"
    assert before == {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}


@pytest.mark.parametrize("mode", ["frame", "isolated"])
def test_registered_legacy_engraving(registered, mode):
    from services.engraving_paths import engraving_paths
    project, registry, instance, record, source, metadata = registered
    paths = engraving_paths(project, source, metadata, mode)
    paths["dir"].mkdir(parents=True)
    content = {"status": "generated", "mode": mode,
               "source": {"silhouette_json": source.relative_to(project).as_posix()},
               "output_png": paths["engraving_png"].relative_to(project).as_posix()}
    paths["metadata"].write_text(json.dumps(content))
    paths["engraving_png"].write_bytes(b"engraving fixture")
    result = resolution.resolve_engraving(project, instance, mode, registry=registry)
    assert result["image"] == paths["engraving_png"]
    assert result["instance_id"] == instance


def test_reclassification_preserves_identity_and_uses_current_registry(registered):
    project, registry, instance, record, source, metadata = registered
    current = install_current_location(registered, field="objects", label="NOTICES board")
    result = resolution.resolve_silhouette(project, instance, registry=registry)
    assert result["metadata"] == current["metadata"] and result["instance_id"] == instance
    assert source.exists()
    with sqlite3.connect(registry) as database:
        assert database.execute("SELECT legacy_json FROM instances").fetchone()[0] == source.relative_to(project).as_posix()


@pytest.mark.parametrize("mode", ["frame", "isolated"])
def test_current_mode_publications(registered, mode):
    project, registry, instance, record, source, metadata = registered
    install_current_location(registered)
    current = install_current_location(registered, "engravings", mode)
    assert resolution.resolve_engraving(project, instance, mode, registry=registry)["image"] == current["image"]


@pytest.mark.parametrize("damage", ["missing", "ambiguous", "stale", "inactive", "wrong_identity"])
def test_current_resolution_failures(registered, damage):
    project, registry, instance, record, source, metadata = registered
    current = install_current_location(registered)
    if damage == "missing":
        current["image"].unlink()
    elif damage == "ambiguous":
        with sqlite3.connect(registry) as database:
            database.execute("INSERT INTO resource_locations SELECT * FROM resource_locations")
    elif damage == "stale":
        with sqlite3.connect(registry) as database:
            database.execute("UPDATE resource_locations SET label='hawk'")
    else:
        content = json.loads(current["metadata"].read_text())
        content.update({"assignment": {"state": "inactive"}} if damage == "inactive" else {"instance_id": "wrong"})
        current["metadata"].write_text(json.dumps(content))
    with pytest.raises((ValueError, FileNotFoundError)):
        resolution.resolve_silhouette(project, instance, registry=registry)


def test_unknown_instance_does_not_allocate(registered):
    project, registry, instance, record, source, metadata = registered
    before = registry.read_bytes()
    with pytest.raises(ValueError):
        resolution.resolve_silhouette(project, instance[:-4] + "0002", registry=registry)
    assert registry.read_bytes() == before


def test_scene_and_annotation_compatibility(tmp_path):
    assert locations.scene_annotation_root(tmp_path) == tmp_path / "data/annotations/scenes"
    from data.annotate import get_annotation_json_path, _load_existing_agg
    from data.index import get_frame_valid_path
    expected = tmp_path / "data/annotations/shots/movie/film.annotations.json"
    assert get_annotation_json_path(tmp_path, "film.mp4", "movie") == expected
    expected.parent.mkdir(parents=True)
    expected.write_text('[{"shot":{"shot_id":"tmdb_1@f000010-f000020"}}]')
    assert "tmdb_1@f000010-f000020" in _load_existing_agg(tmp_path, "film.mp4", "movie")
    assert locations.resolve_annotation(tmp_path, "film.mp4", "movie") == expected
    assert get_frame_valid_path(tmp_path, "film.mp4", "movie").name == "film.frames.valid.npy"


def test_target_annotation_lookup_rejects_duplicate_legacy_copy(registered):
    project, registry, instance, record, source, metadata = registered
    path = locations.annotation_destination(project, record, "movie")
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    assert locations.resolve_annotation(project, record["filename"], "movie", layout="media-first-v1") == path
    legacy = locations.annotation_path(project, record["filename"], "movie")
    legacy.write_text("[]")
    with pytest.raises(ValueError, match="Ambiguous"):
        locations.resolve_annotation(project, record["filename"], "movie", layout="media-first-v1")


@pytest.mark.parametrize("suffix", locations.ANNOTATION_SUFFIXES)
@pytest.mark.parametrize("media_type", ["movie", "gameplay"])
def test_annotation_sidecar_names(tmp_path, suffix, media_type):
    record = {"media_id": "tmdb_1" if media_type == "movie" else "game_rdr2_ce5e0bba", "filename": "original clip.mp4"}
    path = locations.annotation_destination(tmp_path, record, media_type, suffix)
    assert path.name == "original clip" + suffix
    assert not path.exists() and not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", ["", "../escape", "bad/name", "bad\\name", "trailing.", "a" * 256])
def test_unsafe_components_rejected(value):
    with pytest.raises(ValueError):
        locations.component(value)


def test_symlink_and_missing_registry_are_not_followed(registered):
    project, registry, instance, record, source, metadata = registered
    alias = project / "registry-link.sqlite3"
    alias.symlink_to(registry)
    with pytest.raises(ValueError, match="Symlink"):
        resolution.resolve_silhouette(project, instance, registry=alias)
    with pytest.raises(FileNotFoundError):
        resolution.resolve_silhouette(project, instance, registry=project / "missing.sqlite3")
    assert not (project / "missing.sqlite3").exists()


def test_registry_wal_rejected_without_read_sidecars(registered):
    project, registry, instance, record, source, metadata = registered
    with sqlite3.connect(registry) as database:
        database.execute("PRAGMA journal_mode=WAL")
    before = set(project.rglob("*"))
    with pytest.raises(ValueError, match="rollback-journal"):
        resolution.resolve_silhouette(project, instance, registry=registry)
    assert set(project.rglob("*")) == before


def test_duplicate_media_metadata_is_not_guessed(registered):
    project, registry, instance, record, source, metadata = registered
    (project / "data/metadata/movie.json").write_text(json.dumps({"media": [record, record]}))
    with pytest.raises(ValueError, match="ambiguous"):
        locations.find_media(project, "movie", media_id=record["media_id"])


def test_legacy_temporal_correction_preserves_registered_identity(registered):
    project, registry, instance, record, source, metadata = registered
    changed = "tmdb_61044@f078383-f078480"
    source.write_text(json.dumps({**metadata, "shot_id": changed}))
    result = resolution.resolve_silhouette(project, instance, registry=registry)
    assert result["instance_id"] == instance
    assert locations.source_validation(instance, changed)["dirty"]


def test_missing_field_is_not_inferred_during_legacy_lookup(registered):
    project, registry, instance, record, source, metadata = registered
    metadata.pop("field")
    source.write_text(json.dumps(metadata))
    result = resolution.resolve_silhouette(project, instance, registry=registry)
    assert "field" not in result["record"]
    with pytest.raises(ValueError):
        locations.silhouette_destination(project, record, "movie", instance, result["record"].get("field"), "bird")


def test_gameplay_existing_registry_resolution(tmp_path):
    record = {"media_id": "game_rdr2_ce5e0bba", "media_type": "gameplay", "filename_stem": "game clip",
              "filename": "game clip.mp4", "shot_id": "game_rdr2_ce5e0bba@f1012726-f1013511",
              "field": "animals", "label": "bird", "frame": 1012727}
    source = tmp_path / "data/silhouettes/catalog/gameplay/game clip/bird/object_0001.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(record))
    source.with_suffix(".png").write_bytes(b"fixture")
    registry_path = tmp_path / RESEARCH / "game.sqlite3"
    registry = InstanceRegistry(tmp_path, registry_path)
    instance = registry.register([prepare_registration({**record, "path": source}, tmp_path)])[0][1]
    registry.close()
    assert resolution.resolve_silhouette(tmp_path, instance, registry=registry_path)["metadata"] == source


def test_canonical_reference_never_falls_back_to_legacy_path(registered):
    project, registry, instance, record, source, metadata = registered
    current = install_current_location(registered)
    current["metadata"].unlink()
    with pytest.raises(FileNotFoundError):
        resolution.resolve_silhouette(project, instance, registry=registry)
    assert source.exists()