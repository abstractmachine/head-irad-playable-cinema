"""Tests for the atomic-write migration in the cache tier:
`services/vocabulary_index.py` and `services/frame_embeddings.py`.

Both `build_vocabulary_index()` write sites (the empty-cache early return and
the full build) and `build_frame_embeddings()`'s manifest write used to call
``Path.write_text()`` / ``json.dump()`` onto an open file handle directly, so
a crash mid-write could leave a truncated cache file. They now go through
``data.annotate.atomic_write_text()``, which writes to a same-directory temp
file and swaps it in with ``os.replace()``. The binary ``.npy`` writes in
``build_frame_embeddings()`` are unchanged (see report) and not covered here.
"""

import json

import numpy as np
import pytest
from PIL import Image

from data import annotate
from data.annotate import get_annotation_json_path
from services.frame_match import best_frame_path
import services.frame_embeddings as frame_embeddings_mod
from services.frame_embeddings import (
    build_frame_embeddings,
    get_frame_manifest_path,
)
import services.vocabulary_index as vocab_mod
from services.vocabulary_index import build_vocabulary_index


def _simulate_replace_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(annotate.os, "replace", _boom)


def _make_annotation_json(tmp_path, filename, media_type, entries):
    path = get_annotation_json_path(str(tmp_path), filename, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _make_fields_yaml(tmp_path, vocabulary_fields):
    from data.index import save_vocabulary_fields

    save_vocabulary_fields(str(tmp_path), vocabulary_fields)


# ---------------------------------------------------------------------------
# services/vocabulary_index.py
# ---------------------------------------------------------------------------

class TestBuildVocabularyIndex:
    def test_round_trip_with_configured_fields(self, tmp_path):
        _make_fields_yaml(tmp_path, ["animals"])
        _make_annotation_json(
            tmp_path, "Film.mp4", "movie",
            [{"shot": {"shot_id": "a@f000000-f000030", "annotation": {"animals": ["horse"]}}}],
        )

        index = build_vocabulary_index(str(tmp_path), "movie")

        assert index["fields"]["animals"]["horse"]["count"] == 1
        vocab_path = vocab_mod._vocab_path(str(tmp_path), "movie")
        reloaded = json.loads(vocab_path.read_text(encoding="utf-8"))
        assert reloaded["fields"]["animals"]["horse"]["count"] == 1

    def test_empty_allowlist_writes_empty_cache_and_no_leftovers(self, tmp_path):
        _make_annotation_json(tmp_path, "Film.mp4", "movie", [])

        index = build_vocabulary_index(str(tmp_path), "movie")

        assert index["fields"] == {}
        vocab_path = vocab_mod._vocab_path(str(tmp_path), "movie")
        assert vocab_path.exists()
        leftovers = [p for p in vocab_path.parent.iterdir() if p != vocab_path]
        assert leftovers == []

    def test_no_temp_file_left_behind(self, tmp_path):
        _make_fields_yaml(tmp_path, ["animals"])
        _make_annotation_json(
            tmp_path, "Film.mp4", "movie",
            [{"shot": {"shot_id": "a@f000000-f000030", "annotation": {"animals": ["wolf"]}}}],
        )

        build_vocabulary_index(str(tmp_path), "movie")

        vocab_path = vocab_mod._vocab_path(str(tmp_path), "movie")
        leftovers = [p for p in vocab_path.parent.iterdir() if p != vocab_path]
        assert leftovers == []

    def test_cleans_up_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        _make_fields_yaml(tmp_path, ["animals"])
        _make_annotation_json(
            tmp_path, "Film.mp4", "movie",
            [{"shot": {"shot_id": "a@f000000-f000030", "annotation": {"animals": ["wolf"]}}}],
        )
        build_vocabulary_index(str(tmp_path), "movie")
        vocab_path = vocab_mod._vocab_path(str(tmp_path), "movie")
        original = vocab_path.read_text(encoding="utf-8")

        _simulate_replace_failure(monkeypatch)
        with pytest.raises(OSError):
            build_vocabulary_index(str(tmp_path), "movie", force=True)

        assert vocab_path.read_text(encoding="utf-8") == original
        leftovers = [p for p in vocab_path.parent.iterdir() if p != vocab_path]
        assert leftovers == []


# ---------------------------------------------------------------------------
# services/frame_embeddings.py
# ---------------------------------------------------------------------------

def _make_frame_fixture(tmp_path, filename="Film.mp4", media_type="movie", shot_id="a@f000000-f000030"):
    _make_annotation_json(
        tmp_path, filename, media_type,
        [{"shot": {"shot_id": shot_id, "annotation": {}}}],
    )
    png_path = best_frame_path(str(tmp_path), media_type, filename, shot_id)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color="red").save(png_path)


def _stub_model(monkeypatch):
    monkeypatch.setattr(
        frame_embeddings_mod, "load_frame_embedding_model", lambda *a, **k: (None, None, None)
    )
    monkeypatch.setattr(
        frame_embeddings_mod,
        "embed_frame_images",
        lambda images, *a, **k: np.zeros((len(images), 4), dtype="float32"),
    )


class TestBuildFrameEmbeddingsManifestWrite:
    def test_writes_manifest_atomically_and_no_leftovers(self, tmp_path, monkeypatch):
        _make_frame_fixture(tmp_path)
        _stub_model(monkeypatch)

        result = build_frame_embeddings(str(tmp_path), "Film.mp4", "movie")

        assert result["status"] == "ok"
        manifest_path = get_frame_manifest_path(str(tmp_path), "Film.mp4", "movie")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["model"]["name"] == "clip-vit-base-patch32"

        leftovers = [p for p in manifest_path.parent.iterdir() if ".tmp" in p.name]
        assert leftovers == []

    def test_cleans_up_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        _make_frame_fixture(tmp_path)
        _stub_model(monkeypatch)
        build_frame_embeddings(str(tmp_path), "Film.mp4", "movie")

        manifest_path = get_frame_manifest_path(str(tmp_path), "Film.mp4", "movie")
        original = manifest_path.read_text(encoding="utf-8")

        _simulate_replace_failure(monkeypatch)
        with pytest.raises(OSError):
            build_frame_embeddings(str(tmp_path), "Film.mp4", "movie", force=True)

        assert manifest_path.read_text(encoding="utf-8") == original
        leftovers = [p for p in manifest_path.parent.iterdir() if ".tmp" in p.name]
        assert leftovers == []
