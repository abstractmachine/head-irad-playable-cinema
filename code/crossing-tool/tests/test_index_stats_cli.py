"""Tests for `crossing index stats`'s cached-stats CLI behavior.

Covers the "no artifact -> report missing/stale, never silently recompute"
rule at the CLI layer: `index stats` should read the persisted corpus-stats
cache when present and current, report an explicit missing/stale state
(exit 1) when it isn't, and only ever recompute when --force is passed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import pytest
from argparse import Namespace
from unittest.mock import patch

import cli
from services.corpus_stats import CORPUS_STATS_SCHEMA_VERSION, corpus_stats_cache_path


def test_parser_exposes_index_stats_force_flag():
    args = cli.build_parser().parse_args(["index", "stats", "--force"])

    assert args.force is True


def test_parser_index_stats_force_defaults_to_false():
    args = cli.build_parser().parse_args(["index", "stats"])

    assert args.force is False


def test_cli_index_stats_reads_cache_without_recomputing(tmp_path, capsys):
    cached_stats = {"movie_videos": 314, "vocabulary_terms": 8418}

    def _fail(*args, **kwargs):
        raise AssertionError("get_corpus_stats must not run when a valid cache exists")

    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.corpus_stats.get_corpus_stats", side_effect=_fail),
        patch(
            "services.corpus_stats.get_corpus_stats_state",
            return_value={"state": "ready", "stats": cached_stats},
        ),
    ):
        cli._index_stats(Namespace(json=True, verbose=False, force=False))

    assert json.loads(capsys.readouterr().out) == cached_stats


def test_cli_index_stats_missing_cache_reports_missing_without_recomputing(tmp_path, capsys):
    """Without --force, a missing cache must be reported, never silently built."""

    def _fail(*args, **kwargs):
        raise AssertionError("get_corpus_stats must not run when no cache exists")

    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.corpus_stats.get_corpus_stats", side_effect=_fail),
        patch(
            "services.corpus_stats.get_corpus_stats_state",
            return_value={"state": "missing"},
        ),
    ):
        with pytest.raises(SystemExit):
            cli._index_stats(Namespace(json=False, verbose=False, force=False))

    err = capsys.readouterr().err
    assert "have not been built" in err.lower()
    assert "--force" in err


def test_cli_index_stats_stale_cache_reports_stale_without_recomputing(tmp_path, capsys):
    """Without --force, a stale cache must be reported, never silently rebuilt."""

    def _fail(*args, **kwargs):
        raise AssertionError("get_corpus_stats must not run when the cache is stale")

    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.corpus_stats.get_corpus_stats", side_effect=_fail),
        patch(
            "services.corpus_stats.get_corpus_stats_state",
            return_value={"state": "stale", "stats": {"movie_videos": 1}},
        ),
    ):
        with pytest.raises(SystemExit):
            cli._index_stats(Namespace(json=False, verbose=False, force=False))

    err = capsys.readouterr().err
    assert "stale" in err.lower()


def test_cli_index_stats_force_recomputes_even_with_valid_cache(tmp_path, capsys):
    cache_path = corpus_stats_cache_path(str(tmp_path))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"schema_version": CORPUS_STATS_SCHEMA_VERSION, "stats": {"movie_videos": 1}}),
        encoding="utf-8",
    )
    fresh_stats = {"movie_videos": 999}

    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.corpus_stats.get_corpus_stats", return_value=fresh_stats) as get_stats,
    ):
        cli._index_stats(Namespace(json=True, verbose=False, force=True))

    get_stats.assert_called_once_with(str(tmp_path))
    assert json.loads(capsys.readouterr().out) == fresh_stats
    assert json.loads(cache_path.read_text())["stats"] == fresh_stats


@pytest.fixture
def embedding_registry(tmp_path):
    import numpy as np
    from data import resource_locations as locations
    from data.resource_storage import ResourceStore, adopt_registry, digest
    from services.resource_publication import publish_annotation_collection
    from services.silhouette_canonical_identity import InstanceRegistry, RESEARCH

    proposal = InstanceRegistry(tmp_path, tmp_path / RESEARCH / "proposal.sqlite3")
    proposal_path = proposal.path
    proposal.close()
    registry = tmp_path / "data/indexes/resources.sqlite3"
    adopt_registry(tmp_path, proposal_path, registry, exceptions=[], approved=True)
    metadata = tmp_path / "data/metadata/movie.json"
    metadata.parent.mkdir(parents=True)
    records = [{"filename": name + ".mp4", "media_id": "tmdb_" + str(number)}
               for number, name in enumerate(("fresh", "stale", "absent"), 1)]
    metadata.write_text(json.dumps({"media": records}))
    store = ResourceStore(tmp_path, registry, approved=True)
    for record in records:
        filename = record["filename"]
        source = locations.annotation_path(tmp_path, filename, "movie")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps([{"shot": {"shot_id": record["media_id"] + "@f000010-f000020",
                                               "annotation": {"objects": ["field"]}}}]))
        if filename != "absent.mp4":
            for suffix in (".annotations.npy", ".frames.npy"):
                target = locations.annotation_path(tmp_path, filename, "movie", suffix)
                np.save(target, np.ones((1, 2), dtype=np.float32))
                family = "annotation" if suffix == ".annotations.npy" else "frame"
                manifest_suffix = ".annotations.manifest.json" if family == "annotation" else ".frames.manifest.json"
                manifest = {"version": 1, "index_type": family + "-embeddings", "media_type": "movie",
                            "json": {"filename": source.name, "path": str(source.relative_to(tmp_path)),
                                     "hash": "sha256:" + digest(source), "item_count": 1},
                            "npy": {"filename": target.name, "path": str(target.relative_to(tmp_path)),
                                    "hash": "sha256:" + digest(target), "shape": [1, 2], "dtype": "float32"}}
                locations.annotation_path(tmp_path, filename, "movie", manifest_suffix).write_text(json.dumps(manifest))
        store.commit_annotation_layout(record, "movie", "legacy-v1")
    commitment = locations.annotation_commitment(tmp_path, "stale.mp4", "movie", registry)
    source = locations.annotation_path(tmp_path, "stale.mp4", "movie")
    content = json.loads(source.read_text())
    content[0]["shot"]["annotation"]["objects"] = ["road"]
    publish_annotation_collection(tmp_path, registry, "stale.mp4", "movie",
                                  {".annotations.json": json.dumps(content).encode()},
                                  expected_revision=commitment["revision"], expected_hashes=commitment["artifacts"],
                                  approved=True)
    from services.vocabulary_index import build_vocabulary_index
    build_vocabulary_index(str(tmp_path), "movie", registry=registry, force=True)
    return tmp_path, registry


@pytest.mark.parametrize("suffix,counter", [(".annotations.npy", "_count_embeddings"),
                                            (".frames.npy", "_count_frame_embeddings")])
def test_stale_embedding_statistics_preserve_usable_counts(embedding_registry, suffix, counter):
    from data.resource_locations import resolve_annotation
    from services import corpus_stats
    project, registry = embedding_registry
    assert resolve_annotation(project, "fresh.mp4", "movie", suffix, registry=registry).exists()
    with pytest.raises(ValueError, match="Stale derived annotation artifact"):
        resolve_annotation(project, "stale.mp4", "movie", suffix, registry=registry)
    assert getattr(corpus_stats, counter)(project, registry=registry)["movie"] == 1


def test_stale_embedding_refresh_preserves_schema_and_bytes(embedding_registry):
    from data.resource_locations import resolve_annotation
    from services.corpus_stats import refresh_corpus_stats_cache
    from services.corpus_stats import get_corpus_stats_state
    project, registry = embedding_registry
    before = registry.read_bytes(), {str(path): path.read_bytes() for path in (project / "data/annotations").rglob("*") if path.is_file()}
    result = refresh_corpus_stats_cache(project, registry=registry)
    assert result["embeddings"] == 1
    assert result["frame_embeddings"] == 1
    for key in ("annotation_embedding_inventory", "frame_embedding_inventory"):
        assert {name: result[key][name] for name in ("present", "usable", "stale")} == {
            "present": 2, "usable": 1, "stale": 1}
    assert result["embeddings_by_type"] == {"movie": 1}
    assert result["frame_embeddings_by_type"] == {"movie": 1}
    payload = json.loads(corpus_stats_cache_path(project).read_text())
    assert payload["schema_version"] == CORPUS_STATS_SCHEMA_VERSION == 3
    assert get_corpus_stats_state(project, registry=registry) == {"state": "ready", "stats": result}
    assert refresh_corpus_stats_cache(project, registry=registry) == result
    assert before == (registry.read_bytes(), {str(path): path.read_bytes() for path in (project / "data/annotations").rglob("*") if path.is_file()})
    for suffix in (".annotations.npy", ".frames.npy"):
        with pytest.raises(ValueError, match="Stale derived annotation artifact"):
            resolve_annotation(project, "stale.mp4", "movie", suffix, registry=registry)


@pytest.mark.parametrize("suffix", [".annotations.npy", ".frames.npy"])
def test_authenticated_embedding_inventory_states(embedding_registry, suffix):
    from data.resource_locations import annotation_embedding_inventory
    project, registry = embedding_registry
    records = [row for row in annotation_embedding_inventory(project, "movie", registry=registry) if row["suffix"] == suffix]
    assert [(row["filename"], row["state"]) for row in records] == [("fresh.mp4", "usable"), ("stale.mp4", "stale")]
    assert records[1]["source_revision"] == 2 and records[1]["artifact_revision"] == 0


@pytest.mark.parametrize("suffix", [".annotations.npy", ".frames.npy"])
@pytest.mark.parametrize("fault", ["missing", "checksum", "uncommitted"])
def test_embedding_inventory_integrity_failures(embedding_registry, suffix, fault):
    from data import resource_locations as locations
    from services.corpus_stats import _embedding_inventory_stats
    project, registry = embedding_registry
    path = locations.annotation_path(project, "stale.mp4", "movie", suffix)
    if fault == "missing":
        path.unlink()
    elif fault == "checksum":
        path.write_bytes(path.read_bytes() + b"corruption")
    else:
        commitment = locations.annotation_commitment(project, "stale.mp4", "movie", registry)
        artifacts = commitment["artifacts"]
        del artifacts[suffix]
        with closing(sqlite3.connect(registry)) as database, database:
            database.execute("UPDATE annotation_layouts SET artifacts=?,revision=revision+1 WHERE media_id='tmdb_2'",
                             (json.dumps(artifacts),))
    with pytest.raises(ValueError):
        _embedding_inventory_stats(project, registry)


@pytest.mark.parametrize("suffix", [".annotations.manifest.json", ".frames.manifest.json"])
@pytest.mark.parametrize("fault", ["media", "type", "source_path", "source_hash", "member_path", "member_hash"])
def test_embedding_manifest_provenance_fails_closed(embedding_registry, suffix, fault):
    from data import resource_locations as locations
    from data.resource_storage import digest
    from services.corpus_stats import _embedding_inventory_stats
    project, registry = embedding_registry
    path = locations.annotation_path(project, "stale.mp4", "movie", suffix)
    manifest = json.loads(path.read_text())
    if fault == "media":
        manifest["media_type"] = "gameplay"
    elif fault == "type":
        manifest["index_type"] = "unrelated"
    else:
        section = "json" if fault.startswith("source") else "npy"
        member = "path" if fault.endswith("path") else "hash"
        manifest[section][member] = "unrelated"
    path.write_text(json.dumps(manifest))
    commitment = locations.annotation_commitment(project, "stale.mp4", "movie", registry)
    artifacts = {**commitment["artifacts"], suffix: digest(path)}
    with closing(sqlite3.connect(registry)) as database, database:
        database.execute("UPDATE annotation_layouts SET artifacts=?,revision=revision+1 WHERE media_id='tmdb_2'",
                         (json.dumps(artifacts),))
    with pytest.raises(ValueError, match="Invalid embedding manifest provenance"):
        _embedding_inventory_stats(project, registry)


@pytest.mark.parametrize("fault", ["media", "revision", "hash", "source_commitment"])
def test_embedding_publication_provenance_fails_closed(embedding_registry, fault):
    from services.corpus_stats import _embedding_inventory_stats
    project, registry = embedding_registry
    with closing(sqlite3.connect(registry)) as database, database:
        operation, raw = database.execute("SELECT operation_id,details FROM publication_operations WHERE kind='annotation-collection'").fetchone()
        details = json.loads(raw)
        if fault == "media":
            details["media_id"] = "tmdb_foreign"
        elif fault == "revision":
            details["expected_revision"] = 100
        else:
            value = "invalid" if fault == "hash" else "0" * 64
            details["proposed"][".annotations.json"] = value
            details["changed"][".annotations.json"] = value
        database.execute("UPDATE publication_operations SET details=? WHERE operation_id=?", (json.dumps(details), operation))
    with pytest.raises(ValueError):
        _embedding_inventory_stats(project, registry)


def test_embedding_collection_binding_fails_closed(embedding_registry):
    from services.corpus_stats import _embedding_inventory_stats
    project, registry = embedding_registry
    with closing(sqlite3.connect(registry)) as database, database:
        database.execute("UPDATE annotation_layouts SET filename='foreign.mp4',revision=revision+1 WHERE media_id='tmdb_2'")
    with pytest.raises(ValueError, match="filename"):
        _embedding_inventory_stats(project, registry)


def test_embedding_legacy_statistics_compatibility(embedding_registry):
    from services.corpus_stats import get_corpus_stats
    project, registry = embedding_registry
    result = get_corpus_stats(project)
    assert result["embeddings"] == result["frame_embeddings"] == 2
    assert "annotation_embedding_inventory" not in result
    assert "frame_embedding_inventory" not in result


def test_embedding_cached_cli_consumer_compatibility(embedding_registry, monkeypatch, capsys):
    from services.corpus_stats import refresh_corpus_stats_cache, get_corpus_stats_state
    project, registry = embedding_registry
    expected = refresh_corpus_stats_cache(project, registry=registry)
    monkeypatch.setattr(cli.prefs, "get", lambda *args, **kwargs: str(project))
    monkeypatch.setattr("services.corpus_stats.get_corpus_stats_state", lambda *args, **kwargs: get_corpus_stats_state(project, registry=registry))
    cli._index_stats(Namespace(json=True, verbose=False, force=False))
    assert json.loads(capsys.readouterr().out) == expected
