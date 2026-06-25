"""Tests for services.sync_frame_match — catalog loading and frame matching."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _shots_dir(tmp: str, media_type: str) -> Path:
    d = Path(tmp) / "data" / "annotations" / "shots" / media_type
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_npy(path: Path, arr: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(str(path), arr)


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _make_catalog_files(
    tmp: str,
    media_type: str,
    stem: str,
    n_shots: int,
    dim: int = 8,
    n_invalid: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Write .frames.npy, .frames.valid.npy, .annotations.json; return (vecs, valid)."""
    sd = _shots_dir(tmp, media_type)

    # Deterministic unit vectors (one per shot)
    rng  = np.random.default_rng(42)
    vecs = rng.standard_normal((n_shots, dim)).astype("float32")
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = vecs / np.maximum(norms, 1e-9)

    valid = np.ones(n_shots, dtype=bool)
    for i in range(n_invalid):
        valid[i] = False

    _write_npy(sd / f"{stem}.frames.npy",       vecs)
    _write_npy(sd / f"{stem}.frames.valid.npy",  valid)

    annotations = [
        {
            "shot_id":     f"{stem}@f{i:06d}-f{i+30:06d}",
            "start_frame": i * 30,
            "end_frame":   i * 30 + 30,
            "start_time":  f"00:00:{i:02d}.000",
            "end_time":    f"00:00:{i+1:02d}.000",
            "motif":       f"motif_{i}",
            "description": f"Shot {i} description",
            "best_frame":  None,
        }
        for i in range(n_shots)
    ]
    _write_json(sd / f"{stem}.annotations.json", annotations)

    return vecs, valid


# ---------------------------------------------------------------------------
# catalog loading
# ---------------------------------------------------------------------------

class TestLoadFrameCatalog(unittest.TestCase):

    def test_empty_catalog_when_no_files(self):
        from services.sync_frame_match import load_frame_catalog
        with tempfile.TemporaryDirectory() as tmp:
            cat = load_frame_catalog(tmp, "movie", all_items=True, force_reload=True)
            self.assertEqual(cat.size, 0)
            self.assertEqual(cat.media_type, "movie")

    def test_loads_vectors_and_rows(self):
        from services.sync_frame_match import load_frame_catalog
        with tempfile.TemporaryDirectory() as tmp:
            _make_catalog_files(tmp, "movie", "Film One (2000) {tmdb-1}", 5, dim=8)
            cat = load_frame_catalog(tmp, "movie", all_items=True, force_reload=True)
            self.assertEqual(cat.size, 5)
            self.assertEqual(cat.vectors.shape, (5, 8))
            self.assertEqual(len(cat.rows), 5)

    def test_valid_mask_respected(self):
        from services.sync_frame_match import load_frame_catalog
        with tempfile.TemporaryDirectory() as tmp:
            _make_catalog_files(tmp, "movie", "Film Two", 6, dim=8, n_invalid=2)
            cat = load_frame_catalog(tmp, "movie", all_items=True, force_reload=True)
            self.assertEqual(cat.valid_mask.sum(), 4)

    def test_title_filter(self):
        from services.sync_frame_match import load_frame_catalog
        with tempfile.TemporaryDirectory() as tmp:
            _make_catalog_files(tmp, "movie", "Western Film", 3, dim=8)
            _make_catalog_files(tmp, "movie", "Sci-Fi Film",  4, dim=8)
            cat = load_frame_catalog(tmp, "movie", title="Western", force_reload=True)
            self.assertEqual(cat.size, 3)

    def test_gameplay_media_type(self):
        from services.sync_frame_match import load_frame_catalog
        with tempfile.TemporaryDirectory() as tmp:
            _make_catalog_files(tmp, "gameplay", "rdr2", 4, dim=8)
            cat = load_frame_catalog(tmp, "gameplay", all_items=True, force_reload=True)
            self.assertEqual(cat.media_type, "gameplay")
            self.assertEqual(cat.size, 4)

    def test_movies_alias_accepted(self):
        """'movies' should be treated as 'movie' internally."""
        from services.sync_frame_match import load_frame_catalog
        with tempfile.TemporaryDirectory() as tmp:
            _make_catalog_files(tmp, "movie", "Some Film", 3, dim=8)
            cat = load_frame_catalog(tmp, "movies", all_items=True, force_reload=True)
            self.assertEqual(cat.media_type, "movie")
            self.assertEqual(cat.size, 3)

    def test_missing_valid_npy_defaults_to_all_valid(self):
        from services.sync_frame_match import load_frame_catalog
        with tempfile.TemporaryDirectory() as tmp:
            sd = _shots_dir(tmp, "movie")
            vecs = np.ones((3, 8), dtype="float32")
            _write_npy(sd / "NoValidFile.frames.npy", vecs)
            # No .frames.valid.npy written
            cat = load_frame_catalog(tmp, "movie", all_items=True, force_reload=True)
            self.assertEqual(cat.valid_mask.sum(), 3)

    def test_missing_frames_npy_handled_gracefully(self):
        """load_frame_catalog should return an empty catalog, not raise."""
        from services.sync_frame_match import load_frame_catalog
        with tempfile.TemporaryDirectory() as tmp:
            # Only write an annotation JSON, no .frames.npy
            sd = _shots_dir(tmp, "movie")
            _write_json(sd / "Ghost.annotations.json", [])
            cat = load_frame_catalog(tmp, "movie", all_items=True, force_reload=True)
            self.assertEqual(cat.size, 0)


# ---------------------------------------------------------------------------
# match_frame_vector
# ---------------------------------------------------------------------------

class TestMatchFrameVector(unittest.TestCase):

    def test_returns_top_k_sorted_desc(self):
        from services.sync_frame_match import load_frame_catalog, match_frame_vector
        with tempfile.TemporaryDirectory() as tmp:
            vecs, _ = _make_catalog_files(tmp, "movie", "Film", 10, dim=8)
            cat = load_frame_catalog(tmp, "movie", all_items=True, force_reload=True)
            query = vecs[3].copy()   # exact match to row 3
            results = match_frame_vector(query, cat, top=5)
            self.assertEqual(len(results), 5)
            scores = [r["score"] for r in results]
            self.assertEqual(scores, sorted(scores, reverse=True))
            # Best match should be row 3 (cosine similarity ≈ 1.0)
            self.assertAlmostEqual(results[0]["score"], 1.0, places=4)

    def test_filters_invalid_rows(self):
        from services.sync_frame_match import load_frame_catalog, match_frame_vector
        with tempfile.TemporaryDirectory() as tmp:
            vecs, valid = _make_catalog_files(
                tmp, "movie", "Film", 6, dim=8, n_invalid=5)
            # Only row 5 is valid
            cat = load_frame_catalog(tmp, "movie", all_items=True, force_reload=True)
            query = vecs[5].copy()
            results = match_frame_vector(query, cat, top=5)
            # Only 1 valid row → at most 1 result
            self.assertEqual(len(results), 1)
            self.assertAlmostEqual(results[0]["score"], 1.0, places=4)

    def test_empty_catalog_returns_empty(self):
        from services.sync_frame_match import FrameCatalog, match_frame_vector
        cat = FrameCatalog(
            media_type="movie", scope_key="all",
            vectors=np.zeros((0, 8), dtype="float32"),
            valid_mask=np.zeros(0, dtype=bool),
            rows=[],
        )
        query = np.ones(8, dtype="float32")
        self.assertEqual(match_frame_vector(query, cat), [])

    def test_result_dicts_have_required_fields(self):
        from services.sync_frame_match import load_frame_catalog, match_frame_vector
        with tempfile.TemporaryDirectory() as tmp:
            vecs, _ = _make_catalog_files(tmp, "movie", "Film", 3, dim=8)
            cat = load_frame_catalog(tmp, "movie", all_items=True, force_reload=True)
            results = match_frame_vector(vecs[0], cat, top=1)
            self.assertEqual(len(results), 1)
            r = results[0]
            for key in ("rank", "score", "media_type", "title",
                        "filename", "shot_id", "motif", "description"):
                self.assertIn(key, r, msg=f"Missing key: {key}")

    def test_top_k_capped_at_catalog_size(self):
        from services.sync_frame_match import load_frame_catalog, match_frame_vector
        with tempfile.TemporaryDirectory() as tmp:
            vecs, _ = _make_catalog_files(tmp, "movie", "Film", 3, dim=8)
            cat = load_frame_catalog(tmp, "movie", all_items=True, force_reload=True)
            results = match_frame_vector(vecs[0], cat, top=100)
            self.assertLessEqual(len(results), 3)


# ---------------------------------------------------------------------------
# fan-out connection logic (unit-testable helpers)
# ---------------------------------------------------------------------------

class TestConnectionFanOut(unittest.TestCase):
    """Test that _connection_exists and _remove_connection_to_input work correctly."""

    def _make_ws(self):
        """Build a minimal SyncWorkspace-like object for testing the helpers."""
        class _FakeNode:
            def __init__(self, nid):
                self.node_id = nid
            def on_connected(self, src): pass
            def on_disconnected(self): pass

        class _FakeWS:
            def __init__(self):
                self._connections: list[dict] = []
                self._nodes: list = []

            def _connection_exists(self, src_id, src_port, tgt_id, tgt_port):
                return any(
                    c["source_node"] == src_id and c["source_port"] == src_port
                    and c["target_node"] == tgt_id and c["target_port"] == tgt_port
                    for c in self._connections
                )

            def _remove_connection_to_input(self, tgt_id, tgt_port):
                id_map = {n.node_id: n for n in self._nodes}
                kept = []
                for c in self._connections:
                    if c["target_node"] == tgt_id and c["target_port"] == tgt_port:
                        tgt = id_map.get(c["target_node"])
                        if tgt and hasattr(tgt, "on_disconnected"):
                            tgt.on_disconnected()
                    else:
                        kept.append(c)
                self._connections = kept

            def _add(self, src_id, src_port, tgt_id, tgt_port):
                self._remove_connection_to_input(tgt_id, tgt_port)
                if not self._connection_exists(src_id, src_port, tgt_id, tgt_port):
                    self._connections.append({
                        "source_node": src_id, "source_port": src_port,
                        "target_node": tgt_id, "target_port": tgt_port,
                    })

        return _FakeWS, _FakeNode

    def test_one_output_to_two_inputs(self):
        _FakeWS, _FakeNode = self._make_ws()
        ws = _FakeWS()
        src = _FakeNode(1); tgt1 = _FakeNode(2); tgt2 = _FakeNode(3)
        ws._nodes = [src, tgt1, tgt2]
        ws._add(1, "image", 2, "image")
        ws._add(1, "image", 3, "image")
        self.assertEqual(len(ws._connections), 2)

    def test_input_only_keeps_one_source(self):
        _FakeWS, _FakeNode = self._make_ws()
        ws = _FakeWS()
        src1 = _FakeNode(1); src2 = _FakeNode(2); tgt = _FakeNode(3)
        ws._nodes = [src1, src2, tgt]
        ws._add(1, "image", 3, "image")
        ws._add(2, "image", 3, "image")   # replaces the src1→tgt cable
        self.assertEqual(len(ws._connections), 1)
        self.assertEqual(ws._connections[0]["source_node"], 2)

    def test_no_duplicate_connections(self):
        _FakeWS, _FakeNode = self._make_ws()
        ws = _FakeWS()
        src = _FakeNode(1); tgt = _FakeNode(2)
        ws._nodes = [src, tgt]
        ws._add(1, "image", 2, "image")
        ws._add(1, "image", 2, "image")   # duplicate
        self.assertEqual(len(ws._connections), 1)

    def test_remove_input_does_not_remove_other_outputs(self):
        _FakeWS, _FakeNode = self._make_ws()
        ws = _FakeWS()
        src = _FakeNode(1); tgt1 = _FakeNode(2); tgt2 = _FakeNode(3)
        ws._nodes = [src, tgt1, tgt2]
        ws._add(1, "image", 2, "image")
        ws._add(1, "image", 3, "image")
        # Remove tgt2's input only
        ws._remove_connection_to_input(3, "image")
        self.assertEqual(len(ws._connections), 1)
        self.assertEqual(ws._connections[0]["target_node"], 2)


# ---------------------------------------------------------------------------
# audit_catalog
# ---------------------------------------------------------------------------

class TestAuditCatalog(unittest.TestCase):

    def test_empty_when_no_files(self):
        from services.sync_frame_match import audit_catalog
        with tempfile.TemporaryDirectory() as tmp:
            rows = audit_catalog(tmp, "movie")
            self.assertEqual(rows, [])

    def test_reports_vectors_and_valid_count(self):
        from services.sync_frame_match import audit_catalog
        with tempfile.TemporaryDirectory() as tmp:
            _make_catalog_files(tmp, "movie", "Film", 8, dim=8, n_invalid=3)
            rows = audit_catalog(tmp, "movie")
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertIn("filename", r)
            self.assertEqual(r["vectors"], 8)
            self.assertEqual(r["valid_count"], 5)

    def test_missing_annotation_flagged(self):
        from services.sync_frame_match import audit_catalog
        with tempfile.TemporaryDirectory() as tmp:
            sd = _shots_dir(tmp, "movie")
            vecs = np.ones((4, 8), dtype="float32")
            _write_npy(sd / "NoAnn.frames.npy", vecs)
            # No .annotations.json written
            rows = audit_catalog(tmp, "movie")
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["missing_annotation"])

    def test_annotation_present_flagged_ok(self):
        from services.sync_frame_match import audit_catalog
        with tempfile.TemporaryDirectory() as tmp:
            _make_catalog_files(tmp, "movie", "FilmWithAnn", 3, dim=8)
            rows = audit_catalog(tmp, "movie")
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["missing_annotation"])

    def test_movies_alias_accepted(self):
        from services.sync_frame_match import audit_catalog
        with tempfile.TemporaryDirectory() as tmp:
            _make_catalog_files(tmp, "movie", "Film", 4, dim=8)
            rows = audit_catalog(tmp, "movies")
            self.assertEqual(len(rows), 1)

    def test_gameplay_media_type(self):
        from services.sync_frame_match import audit_catalog
        with tempfile.TemporaryDirectory() as tmp:
            _make_catalog_files(tmp, "gameplay", "rdr2", 6, dim=8)
            _make_catalog_files(tmp, "movie", "SomeFilm", 3, dim=8)
            rows = audit_catalog(tmp, "gameplay")
            self.assertEqual(len(rows), 1)
            self.assertIn("rdr2", rows[0]["filename"])


# ---------------------------------------------------------------------------
# match_image_path (integration test — uses a tiny synthetic image,
# bypasses CLIP by mocking embed_rgb_frame)
# ---------------------------------------------------------------------------

class TestMatchImagePath(unittest.TestCase):

    def _write_png(self, path: Path, w: int = 8, h: int = 8) -> None:
        """Write a tiny solid-colour PNG."""
        from PIL import Image
        img = Image.new("RGB", (w, h), color=(128, 64, 32))
        img.save(str(path))

    def test_match_image_path_calls_service(self):
        """match_image_path should return a list of dicts with rank/score."""
        import unittest.mock as mock
        from services import sync_frame_match as sfm

        rng  = np.random.default_rng(0)
        dim  = 8
        query_vec = rng.standard_normal(dim).astype("float32")
        query_vec /= np.linalg.norm(query_vec) + 1e-9

        with tempfile.TemporaryDirectory() as tmp:
            _make_catalog_files(tmp, "movie", "TestFilm", 5, dim=dim)

            img_path = Path(tmp) / "frame.png"
            self._write_png(img_path)

            # Patch embed_rgb_frame so no CLIP model is needed
            with mock.patch(
                "services.frame_vector.embed_rgb_frame",
                return_value=query_vec,
            ), mock.patch(
                "services.frame_vector.load_frame_vector_model",
                return_value=(None, None, None, "clip-vit-base-patch32"),
            ):
                from services.sync_frame_match import _catalog_cache
                _catalog_cache.clear()
                results = sfm.match_image_path(
                    img_path, tmp, "movie", all_items=True,
                )

            # Basic structure checks
            self.assertIsInstance(results, list)
            self.assertGreater(len(results), 0)
            r = results[0]
            self.assertIn("rank",  r)
            self.assertIn("score", r)
            self.assertIn("title", r)


if __name__ == "__main__":
    unittest.main()
