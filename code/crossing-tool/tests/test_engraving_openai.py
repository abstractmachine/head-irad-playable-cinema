"""Tests for services/keys.py and services/engraving_generate_openai.py.

Covers:
- get_key: returns stored key, raises MissingKeyError when absent or no project
- _expand_prompt: substitutes known variables, leaves unknown ones unchanged
- generate_engraving_openai:
    - writes raw.png and engraving.png (identical on first pass)
    - updates engraving.json to status="generated" with generation fields
    - updates request.json to status="sent"
    - raises FileExistsError without --force when raw.png exists
    - succeeds with force=True on a re-run
    - raises MissingKeyError when the OpenAI key file is absent
    - raises FileNotFoundError when the silhouette PNG is missing
    - engraving.json contains no prompt text (provenance only)

All OpenAI network calls are replaced by a mock returning minimal PNG bytes.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from services.engraving_paths import ENGRAVING_SCHEMA_VERSION
from services.engraving_generate_openai import _expand_prompt
from services.keys import MissingKeyError, get_key


# ---------------------------------------------------------------------------
# Helpers (shared with test_engraving_prepare but duplicated to stay isolated)
# ---------------------------------------------------------------------------

_FAKE_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # minimal fake PNG header


def _make_project(tmp: Path) -> Path:
    """Minimal project dir with an engraving prompt and an OpenAI key."""
    project = tmp / "project"
    prompts_dir = project / "prompts" / "engravings"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "engravings-2026-01-01-v1.txt").write_text(
        "Render $label ($field) as an engraving.", encoding="utf-8"
    )
    key_dir = project / "preferences" / "keys"
    key_dir.mkdir(parents=True)
    (key_dir / "openai_api_key.txt").write_text("sk-test-fake", encoding="utf-8")
    return project


def _make_silhouette(label_dir: Path, index: int = 1) -> Path:
    """Write a minimal object PNG + JSON pair and return the JSON path."""
    label_dir.mkdir(parents=True, exist_ok=True)
    stem = f"object_{index:04d}"

    png_path = label_dir / f"{stem}.png"
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 128))
    img.save(png_path)

    meta = {
        "media_type": "movie",
        "filename": "Test Film (1934) {tmdb-000001}.mp4",
        "filename_stem": "Test Film (1934) {tmdb-000001}",
        "media_id": "tmdb_1",
        "shot_id": "tmdb_1@f000100-f000200",
        "frame": 150,
        "label": "horse",
        "field": "animals",
        "confidence": 0.90,
        "bbox": [10, 20, 30, 40],
        "mask_area": 1200,
        "frame_size": [1920, 1080],
        "source_frame": "",
        "png": f"{stem}.png",
    }
    json_path = label_dir / f"{stem}.json"
    json_path.write_text(json.dumps(meta), encoding="utf-8")
    return json_path


# ---------------------------------------------------------------------------
# get_key
# ---------------------------------------------------------------------------

class TestGetKey(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = _make_project(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_stored_key(self):
        key = get_key("openai", str(self.project))
        self.assertEqual(key, "sk-test-fake")

    def test_strips_whitespace(self):
        key_file = self.project / "preferences" / "keys" / "openai_api_key.txt"
        key_file.write_text("  sk-spaces  \n", encoding="utf-8")
        self.assertEqual(get_key("openai", str(self.project)), "sk-spaces")

    def test_missing_key_raises(self):
        (self.project / "preferences" / "keys" / "openai_api_key.txt").unlink()
        with self.assertRaises(MissingKeyError):
            get_key("openai", str(self.project))

    def test_no_project_raises(self):
        with patch("tool.prefs.get", return_value=None):
            with self.assertRaises(MissingKeyError):
                get_key("openai")

    def test_unknown_service_raises(self):
        with self.assertRaises(MissingKeyError):
            get_key("nonexistent_service", str(self.project))


# ---------------------------------------------------------------------------
# _expand_prompt
# ---------------------------------------------------------------------------

class TestExpandPrompt(unittest.TestCase):
    def test_substitutes_label_and_field(self):
        meta = {"label": "horse", "field": "animals"}
        result = _expand_prompt("Draw $label in $field", meta)
        self.assertEqual(result, "Draw horse in animals")

    def test_unknown_placeholders_left_unchanged(self):
        meta = {"label": "horse"}
        result = _expand_prompt("$label — $unknown_var", meta)
        self.assertIn("horse", result)
        self.assertIn("$unknown_var", result)

    def test_missing_meta_gives_empty_string(self):
        result = _expand_prompt("$label $field $shot_id", {})
        self.assertEqual(result.strip(), "")

    def test_all_standard_vars_expanded(self):
        meta = {
            "label": "L", "field": "F", "shot_id": "S",
            "media_id": "M", "filename_stem": "FS",
            "filename": "FN", "frame": 42,
        }
        result = _expand_prompt(
            "$label $field $shot_id $media_id $filename_stem $filename $frame", meta
        )
        self.assertEqual(result, "L F S M FS FN 42")


# ---------------------------------------------------------------------------
# generate_engraving_openai  (OpenAI call mocked)
# ---------------------------------------------------------------------------

class TestGenerateEngravingOpenAI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = _make_project(self.tmp)
        self.label_dir = self.tmp / "sil" / "horse"
        self.json_path = _make_silhouette(self.label_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **kwargs):
        """Call generate_engraving_openai with the OpenAI API mocked."""
        from services.engraving_generate_openai import generate_engraving_openai
        with patch(
            "services.engraving_generate_openai._call_openai_api",
            return_value=_FAKE_PNG_BYTES,
        ):
            return generate_engraving_openai(
                str(self.project), self.json_path, **kwargs
            )

    # ── file creation ────────────────────────────────────────────────────────

    def test_writes_raw_png(self):
        result = self._run()
        self.assertTrue(result["raw_png"].exists())

    def test_writes_engraving_png(self):
        result = self._run()
        self.assertTrue(result["engraving_png"].exists())

    def test_raw_and_engraving_png_same_content(self):
        result = self._run()
        self.assertEqual(
            result["raw_png"].read_bytes(),
            result["engraving_png"].read_bytes(),
        )

    def test_raw_png_content_matches_api_response(self):
        result = self._run()
        self.assertEqual(result["raw_png"].read_bytes(), _FAKE_PNG_BYTES)

    # ── engraving.json ────────────────────────────────────────────────────────

    def test_engraving_json_status_generated(self):
        result = self._run()
        data = json.loads(result["metadata"].read_text())
        self.assertEqual(data["status"], "generated")

    def test_engraving_json_generation_fields(self):
        result = self._run(model="gpt-image-2", size="1024x1024")
        data = json.loads(result["metadata"].read_text())
        gen = data["generation"]
        self.assertEqual(gen["service"], "openai")
        self.assertEqual(gen["model"], "gpt-image-2")
        self.assertEqual(gen["api"], "images.edit")
        self.assertEqual(gen["size"], "1024x1024")
        self.assertIsNotNone(gen["created"])

    def test_engraving_json_no_prompt_text(self):
        result = self._run()
        raw = result["metadata"].read_text()
        self.assertNotIn("Render ", raw)  # prompt template text must not appear

    def test_engraving_json_schema_version_preserved(self):
        result = self._run()
        data = json.loads(result["metadata"].read_text())
        self.assertEqual(data["schema_version"], ENGRAVING_SCHEMA_VERSION)

    # ── request.json ─────────────────────────────────────────────────────────

    def test_request_json_status_sent(self):
        result = self._run()
        req = json.loads((result["dir"] / "request.json").read_text())
        self.assertEqual(req["status"], "sent")

    def test_request_json_service_and_model(self):
        result = self._run(model="gpt-image-2")
        req = json.loads((result["dir"] / "request.json").read_text())
        self.assertEqual(req["service"], "openai")
        self.assertEqual(req["model"], "gpt-image-2")

    def test_request_json_no_prompt_text(self):
        result = self._run()
        raw = (result["dir"] / "request.json").read_text()
        self.assertNotIn("Render ", raw)

    # ── force / overwrite guards ─────────────────────────────────────────────

    def test_raises_file_exists_without_force(self):
        self._run()  # first run
        with self.assertRaises(FileExistsError):
            self._run()  # second run without force

    def test_force_overwrites(self):
        self._run()
        result = self._run(force=True)
        self.assertTrue(result["raw_png"].exists())

    # ── error cases ──────────────────────────────────────────────────────────

    def test_missing_key_raises(self):
        from services.engraving_generate_openai import generate_engraving_openai
        (self.project / "preferences" / "keys" / "openai_api_key.txt").unlink()
        with self.assertRaises(MissingKeyError):
            with patch("services.engraving_generate_openai._call_openai_api",
                       return_value=_FAKE_PNG_BYTES):
                generate_engraving_openai(str(self.project), self.json_path)

    def test_missing_silhouette_png_raises(self):
        from services.engraving_generate_openai import generate_engraving_openai
        (self.label_dir / "object_0001.png").unlink()
        meta = json.loads(self.json_path.read_text())
        del meta["png"]
        self.json_path.write_text(json.dumps(meta))
        with self.assertRaises(FileNotFoundError):
            with patch("services.engraving_generate_openai._call_openai_api",
                       return_value=_FAKE_PNG_BYTES):
                generate_engraving_openai(str(self.project), self.json_path)

    # ── return dict ──────────────────────────────────────────────────────────

    def test_return_keys(self):
        result = self._run()
        for key in ("dir", "raw_png", "engraving_png", "metadata",
                    "expanded_prompt_length", "model", "size", "project"):
            self.assertIn(key, result)

    def test_expanded_prompt_length_nonzero(self):
        result = self._run()
        self.assertGreater(result["expanded_prompt_length"], 0)

    def test_api_called_with_correct_model_and_size(self):
        from services.engraving_generate_openai import generate_engraving_openai
        with patch(
            "services.engraving_generate_openai._call_openai_api",
            return_value=_FAKE_PNG_BYTES,
        ) as mock_api:
            generate_engraving_openai(
                str(self.project), self.json_path,
                model="gpt-image-1", size="1024x1792",
            )
        mock_api.assert_called_once()
        _, _, _, called_model, called_size = mock_api.call_args.args
        self.assertEqual(called_model, "gpt-image-1")
        self.assertEqual(called_size, "1024x1792")
