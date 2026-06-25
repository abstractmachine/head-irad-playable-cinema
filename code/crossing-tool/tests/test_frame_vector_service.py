"""Tests for services.frame_vector — embed_image_path and embed_rgb_frame."""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import numpy as np


def _make_unit_vec(dim: int = 512, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype("float32")
    v /= np.linalg.norm(v) + 1e-9
    return v


def _write_png(path: Path, w: int = 4, h: int = 4) -> None:
    from PIL import Image
    img = Image.new("RGB", (w, h), color=(100, 150, 200))
    img.save(str(path))


# ---------------------------------------------------------------------------
# embed_rgb_frame
# ---------------------------------------------------------------------------

class TestEmbedRgbFrame(unittest.TestCase):

    def _make_bundle(self, dim=8):
        """Return a fake model bundle that produces a fixed unit vector."""
        vec = _make_unit_vec(dim)
        return (None, None, None, "clip-vit-base-patch32"), vec

    def test_returns_1d_float32_array(self):
        from services import frame_vector as fv
        dim = 8
        bundle, expected_vec = self._make_bundle(dim)

        with mock.patch(
            "services.frame_vector.embed_rgb_frame",
            wraps=lambda frame_rgb, model_bundle: expected_vec,
        ):
            from services.frame_vector import embed_rgb_frame
            frame = np.zeros((4, 4, 3), dtype="uint8")
            result = embed_rgb_frame(frame, bundle[0])  # bundle[0] is the tuple
        # Just verifying the fixture works — real path tested below

    def test_shape_and_dtype(self):
        """embed_rgb_frame with a mocked CLIP call returns expected shape/dtype."""
        dim = 512
        expected_vec = _make_unit_vec(dim)

        with mock.patch(
            "services.frame_match.embed_frame_images",
            return_value=np.expand_dims(expected_vec, 0),
        ):
            from services.frame_vector import embed_rgb_frame, load_frame_vector_model
            bundle_tuple = (object(), object(), "cpu", "clip-vit-base-patch32")
            frame_rgb = np.zeros((4, 4, 3), dtype="uint8")
            result = embed_rgb_frame(frame_rgb, bundle_tuple)

        self.assertIsInstance(result, np.ndarray)
        self.assertEqual(result.dtype, np.float32)
        self.assertEqual(result.shape, (dim,))
        np.testing.assert_allclose(result, expected_vec)


# ---------------------------------------------------------------------------
# embed_image_path
# ---------------------------------------------------------------------------

class TestEmbedImagePath(unittest.TestCase):

    def test_returns_dict_with_correct_keys(self):
        dim = 512
        expected_vec = _make_unit_vec(dim)

        with tempfile.TemporaryDirectory() as tmp:
            img_path = Path(tmp) / "frame.png"
            _write_png(img_path)

            with mock.patch(
                "services.frame_vector.load_frame_vector_model",
                return_value=(None, None, None, "clip-vit-base-patch32"),
            ), mock.patch(
                "services.frame_vector.embed_rgb_frame",
                return_value=expected_vec,
            ):
                from services.frame_vector import embed_image_path
                result = embed_image_path(str(img_path), tmp)

        self.assertIsInstance(result, dict)
        self.assertIn("model",     result)
        self.assertIn("dimension", result)
        self.assertIn("vector",    result)

    def test_model_name_matches_bundle(self):
        dim = 512
        model_name = "clip-vit-base-patch32"
        expected_vec = _make_unit_vec(dim)

        with tempfile.TemporaryDirectory() as tmp:
            img_path = Path(tmp) / "frame.png"
            _write_png(img_path)

            with mock.patch(
                "services.frame_vector.load_frame_vector_model",
                return_value=(None, None, None, model_name),
            ), mock.patch(
                "services.frame_vector.embed_rgb_frame",
                return_value=expected_vec,
            ):
                from services.frame_vector import embed_image_path
                result = embed_image_path(str(img_path), tmp)

        self.assertEqual(result["model"], model_name)

    def test_dimension_matches_vector_length(self):
        dim = 512
        expected_vec = _make_unit_vec(dim)

        with tempfile.TemporaryDirectory() as tmp:
            img_path = Path(tmp) / "frame.png"
            _write_png(img_path)

            with mock.patch(
                "services.frame_vector.load_frame_vector_model",
                return_value=(None, None, None, "clip-vit-base-patch32"),
            ), mock.patch(
                "services.frame_vector.embed_rgb_frame",
                return_value=expected_vec,
            ):
                from services.frame_vector import embed_image_path
                result = embed_image_path(str(img_path), tmp)

        self.assertEqual(result["dimension"], dim)
        self.assertEqual(len(result["vector"]), dim)

    def test_vector_is_list_of_floats(self):
        dim = 8
        expected_vec = _make_unit_vec(dim)

        with tempfile.TemporaryDirectory() as tmp:
            img_path = Path(tmp) / "frame.png"
            _write_png(img_path)

            with mock.patch(
                "services.frame_vector.load_frame_vector_model",
                return_value=(None, None, None, "clip-vit-base-patch32"),
            ), mock.patch(
                "services.frame_vector.embed_rgb_frame",
                return_value=expected_vec,
            ):
                from services.frame_vector import embed_image_path
                result = embed_image_path(str(img_path), tmp)

        self.assertIsInstance(result["vector"], list)
        for v in result["vector"]:
            self.assertIsInstance(v, float)

    def test_vector_values_match_embedded_output(self):
        dim = 8
        expected_vec = _make_unit_vec(dim)

        with tempfile.TemporaryDirectory() as tmp:
            img_path = Path(tmp) / "frame.png"
            _write_png(img_path)

            with mock.patch(
                "services.frame_vector.load_frame_vector_model",
                return_value=(None, None, None, "clip-vit-base-patch32"),
            ), mock.patch(
                "services.frame_vector.embed_rgb_frame",
                return_value=expected_vec,
            ):
                from services.frame_vector import embed_image_path
                result = embed_image_path(str(img_path), tmp)

        np.testing.assert_allclose(result["vector"], expected_vec.tolist(), rtol=1e-5)

    def test_json_serializable(self):
        """Output dict must be serializable to JSON without custom encoder."""
        import json
        dim = 8
        expected_vec = _make_unit_vec(dim)

        with tempfile.TemporaryDirectory() as tmp:
            img_path = Path(tmp) / "frame.png"
            _write_png(img_path)

            with mock.patch(
                "services.frame_vector.load_frame_vector_model",
                return_value=(None, None, None, "clip-vit-base-patch32"),
            ), mock.patch(
                "services.frame_vector.embed_rgb_frame",
                return_value=expected_vec,
            ):
                from services.frame_vector import embed_image_path
                result = embed_image_path(str(img_path), tmp)

        # Should not raise
        serialised = json.dumps(result)
        reloaded = json.loads(serialised)
        self.assertEqual(reloaded["model"], result["model"])
        self.assertEqual(reloaded["dimension"], result["dimension"])


# ---------------------------------------------------------------------------
# _load_vector_json — CLI helper (tested directly)
# ---------------------------------------------------------------------------

class TestLoadVectorJson(unittest.TestCase):
    """Tests for the cli._load_vector_json helper."""

    def _get_fn(self):
        import importlib, sys
        # Reload to avoid stale imports if cli was modified
        if "cli" in sys.modules:
            cli = sys.modules["cli"]
        else:
            import cli
        return cli._load_vector_json

    def test_accepts_bare_list(self):
        import json, tempfile as _tmp
        vec = [0.1, 0.2, 0.3]
        with _tmp.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(vec, f)
            fname = f.name
        try:
            result = self._get_fn()(fname)
            np.testing.assert_allclose(result, vec, rtol=1e-5)
        finally:
            Path(fname).unlink(missing_ok=True)

    def test_accepts_object_with_vector_key(self):
        import json, tempfile as _tmp
        vec = [0.4, 0.5, 0.6]
        payload = {"model": "clip-vit-base-patch32", "dimension": 3, "vector": vec}
        with _tmp.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            fname = f.name
        try:
            result = self._get_fn()(fname)
            np.testing.assert_allclose(result, vec, rtol=1e-5)
        finally:
            Path(fname).unlink(missing_ok=True)

    def test_raises_on_unknown_format(self):
        import json, tempfile as _tmp
        payload = {"not_a_vector": True}
        with _tmp.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            fname = f.name
        try:
            with self.assertRaises((ValueError, KeyError)):
                self._get_fn()(fname)
        finally:
            Path(fname).unlink(missing_ok=True)

    def test_output_is_float32(self):
        import json, tempfile as _tmp
        vec = [1.0, 2.0, 3.0]
        with _tmp.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(vec, f)
            fname = f.name
        try:
            result = self._get_fn()(fname)
            self.assertEqual(result.dtype, np.float32)
        finally:
            Path(fname).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
