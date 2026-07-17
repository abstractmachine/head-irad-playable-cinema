"""Tests for the mode-aware engraving pipeline.

Covers:
- Mode-specific prompt loading (silhouette / full)
- Fallback to last file when no mode-prefixed files exist
- Unknown mode raises ValueError
- engraving_output_filename: includes movie stub, frame id, object id, mode
- engraving_paths: mode sub-directory layout
- engraving_is_generated: checks raw.png presence
- _expand_prompt: motif and human_best fields propagated
- generate_engraving_openai:
    - silhouette mode uses single-image API call
    - full mode calls dual-image API
    - both mode runs silhouette then full in sequence
    - each mode uses the correct prompt file
    - FileExistsError without --force; force=True regenerates
    - mode stored in engraving.json and request.json
    - engraving_png has mode-specific filename
- scan_best_silhouettes: only returns human_best=True objects
- batch_generate_engravings:
    - skips already-generated entries
    - force=True regenerates
    - both mode generates two passes per target
    - failed items counted correctly
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from PIL import Image

from services.engraving_paths import (
    engraving_is_generated,
    engraving_output_filename,
    engraving_paths,
)
from services.engraving_prompt import (
    EngravingPromptError,
    load_engraving_prompt,
)
from services.engraving_generate_openai import _expand_prompt

_FAKE_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp: Path, *, with_isolated_prompt=True, with_frame_prompt=True) -> Path:
    """Minimal project with mode-specific prompt files and an OpenAI key."""
    project = tmp / "project"
    prompts_dir = project / "prompts" / "engravings"
    prompts_dir.mkdir(parents=True)
    if with_isolated_prompt:
        (prompts_dir / "engravings-isolated-2026-01-01-v1.txt").write_text(
            "Isolated engraving: $label ($field) motif=$motif",
            encoding="utf-8",
        )
    if with_frame_prompt:
        (prompts_dir / "engravings-frame-2026-01-01-v1.txt").write_text(
            "Frame engraving: $label from $filename_stem motif=$motif",
            encoding="utf-8",
        )
    key_dir = project / "preferences" / "keys"
    key_dir.mkdir(parents=True)
    (key_dir / "openai_api_key.txt").write_text("sk-test-fake", encoding="utf-8")
    return project


def _make_silhouette(
    label_dir: Path,
    index: int = 1,
    *,
    human_best: bool = False,
    source_frame: str = "",
    motif: str = "cowboy at sunset",
    label: str = "horse",
) -> Path:
    """Write a minimal object PNG + JSON pair and return the JSON path."""
    label_dir.mkdir(parents=True, exist_ok=True)
    stem = f"object_{index:04d}"

    png_path = label_dir / f"{stem}.png"
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 128))
    img.save(png_path)

    meta: dict = {
        "media_type": "movie",
        "filename": "Django (1966) {tmdb-11969}.mp4",
        "filename_stem": "Django (1966) {tmdb-11969}",
        "media_id": "tmdb_11969",
        "shot_id": "tmdb_11969@f001240-f001310",
        "frame": 1275,
        "label": label,
        "field": "animals",
        "confidence": 0.91,
        "bbox": [10, 20, 30, 40],
        "mask_area": 12345,
        "frame_size": [1920, 1080],
        "source_frame": source_frame,
        "png": f"{stem}.png",
        "motif": motif,
    }
    if human_best:
        meta["human_best"] = True

    json_path = label_dir / f"{stem}.json"
    json_path.write_text(json.dumps(meta), encoding="utf-8")
    return json_path


def _make_source_frame_png(tmp: Path) -> Path:
    """Write a minimal full-frame PNG and return its path."""
    frame_path = tmp / "frame_001275.png"
    img = Image.new("RGB", (100, 100), (128, 128, 128))
    img.save(frame_path)
    return frame_path


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

class TestLoadEngravingPromptModeAware(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_silhouette_mode_picks_silhouette_file(self):
        project = _make_project(self.tmp)
        name, text = load_engraving_prompt(str(project), mode="isolated")
        self.assertIn("isolated", name)
        self.assertIn("Isolated engraving", text)

    def test_full_mode_picks_full_file(self):
        project = _make_project(self.tmp)
        name, text = load_engraving_prompt(str(project), mode="frame")
        self.assertIn("frame", name)
        self.assertIn("Frame engraving", text)

    def test_silhouette_and_full_use_different_files(self):
        project = _make_project(self.tmp)
        sil_name, _ = load_engraving_prompt(str(project), mode="isolated")
        full_name, _ = load_engraving_prompt(str(project), mode="frame")
        self.assertNotEqual(sil_name, full_name)

    def test_fallback_to_last_file_when_no_mode_prefix(self):
        """When no mode-prefixed file exists, falls back to alphabetically last."""
        project = _make_project(self.tmp, with_isolated_prompt=False, with_frame_prompt=False)
        prompts_dir = project / "prompts" / "engravings"
        (prompts_dir / "engravings-2026-01-01-v1.txt").write_text("Generic prompt", encoding="utf-8")
        name, text = load_engraving_prompt(str(project), mode="isolated")
        self.assertEqual(text, "Generic prompt")

    def test_unknown_mode_raises_value_error(self):
        project = _make_project(self.tmp)
        with self.assertRaises(ValueError):
            load_engraving_prompt(str(project), mode="watercolor")

    def test_missing_prompts_dir_raises(self):
        project = self.tmp / "empty_project"
        project.mkdir()
        with self.assertRaises(EngravingPromptError):
            load_engraving_prompt(str(project), mode="isolated")

    def test_latest_version_selected_when_multiple(self):
        """Alphabetically last versioned file within a mode is selected."""
        project = _make_project(self.tmp)
        prompts_dir = project / "prompts" / "engravings"
        (prompts_dir / "engravings-isolated-2026-07-01-v2.txt").write_text(
            "Newer isolated prompt", encoding="utf-8"
        )
        name, text = load_engraving_prompt(str(project), mode="isolated")
        self.assertIn("v2", name)
        self.assertEqual(text, "Newer isolated prompt")


# ---------------------------------------------------------------------------
# Output filename
# ---------------------------------------------------------------------------

class TestEngravingOutputFilename(unittest.TestCase):
    def _make_json(self, tmp: Path) -> tuple[Path, dict]:
        label_dir = tmp / "horse"
        label_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "filename_stem": "Django (1966) {tmdb-11969}",
            "frame": 1275,
        }
        json_path = label_dir / "object_0007.json"
        json_path.write_text(json.dumps(meta), encoding="utf-8")
        return json_path, meta

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_contains_movie_stub(self):
        json_path, meta = self._make_json(self.tmp)
        name = engraving_output_filename(json_path, meta, "isolated")
        self.assertIn("django", name.lower())

    def test_contains_frame_id(self):
        json_path, meta = self._make_json(self.tmp)
        name = engraving_output_filename(json_path, meta, "isolated")
        self.assertIn("f001275", name)

    def test_contains_object_id(self):
        json_path, meta = self._make_json(self.tmp)
        name = engraving_output_filename(json_path, meta, "isolated")
        self.assertIn("object_0007", name)

    def test_contains_mode_silhouette(self):
        json_path, meta = self._make_json(self.tmp)
        name = engraving_output_filename(json_path, meta, "isolated")
        self.assertTrue(name.endswith("-isolated.png"))

    def test_contains_mode_full(self):
        json_path, meta = self._make_json(self.tmp)
        name = engraving_output_filename(json_path, meta, "frame")
        self.assertTrue(name.endswith("-frame.png"))

    def test_silhouette_and_full_names_differ(self):
        json_path, meta = self._make_json(self.tmp)
        sil = engraving_output_filename(json_path, meta, "isolated")
        full = engraving_output_filename(json_path, meta, "frame")
        self.assertNotEqual(sil, full)


# ---------------------------------------------------------------------------
# engraving_paths and engraving_is_generated
# ---------------------------------------------------------------------------

class TestEngravingPathsMode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        label_dir = self.tmp / "horse"
        self.json_path = _make_silhouette(label_dir)
        self.meta = json.loads(self.json_path.read_text())

    def tearDown(self):
        self._tmp.cleanup()

    def test_silhouette_paths_in_silhouette_subdir(self):
        paths = engraving_paths(str(self.tmp), self.json_path, self.meta, "isolated")
        self.assertIn("isolated", str(paths["dir"]))

    def test_full_paths_in_full_subdir(self):
        paths = engraving_paths(str(self.tmp), self.json_path, self.meta, "frame")
        self.assertIn("frame", str(paths["dir"]))

    def test_silhouette_and_full_dirs_different(self):
        sil = engraving_paths(str(self.tmp), self.json_path, self.meta, "isolated")["dir"]
        full = engraving_paths(str(self.tmp), self.json_path, self.meta, "frame")["dir"]
        self.assertNotEqual(sil, full)

    def test_is_generated_false_when_no_raw_png(self):
        self.assertFalse(
            engraving_is_generated(str(self.tmp), self.json_path, self.meta, "isolated")
        )

    def test_is_generated_true_when_raw_png_exists(self):
        paths = engraving_paths(str(self.tmp), self.json_path, self.meta, "isolated")
        paths["dir"].mkdir(parents=True, exist_ok=True)
        paths["raw_png"].write_bytes(_FAKE_PNG_BYTES)
        self.assertTrue(
            engraving_is_generated(str(self.tmp), self.json_path, self.meta, "isolated")
        )


# ---------------------------------------------------------------------------
# _expand_prompt — motif propagation
# ---------------------------------------------------------------------------

class TestExpandPromptMotif(unittest.TestCase):
    def test_motif_expanded(self):
        meta = {"label": "horse", "motif": "cowboy at dusk"}
        result = _expand_prompt("$label — motif: $motif", meta)
        self.assertEqual(result, "horse — motif: cowboy at dusk")

    def test_human_best_expanded(self):
        meta = {"label": "horse", "human_best": True}
        result = _expand_prompt("$label human_best=$human_best", meta)
        self.assertIn("True", result)

    def test_missing_motif_gives_empty(self):
        result = _expand_prompt("$label $motif", {"label": "horse"})
        self.assertEqual(result.strip(), "horse")


# ---------------------------------------------------------------------------
# generate_engraving_openai — mode dispatch
# ---------------------------------------------------------------------------

class TestGenerateEngravingOpenAIModes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = _make_project(self.tmp)
        self.label_dir = self.tmp / "sil" / "horse"
        frame_png = _make_source_frame_png(self.tmp)
        self.json_path = _make_silhouette(
            self.label_dir,
            source_frame=str(frame_png),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, mode="isolated", force=False, mock_single=None, mock_dual=None):
        from services.engraving_generate_openai import generate_engraving_openai
        single_ret = mock_single if mock_single is not None else _FAKE_PNG_BYTES
        dual_ret = mock_dual if mock_dual is not None else _FAKE_PNG_BYTES
        with patch(
            "services.engraving_generate_openai._call_openai_api",
            return_value=single_ret,
        ) as mock_s, patch(
            "services.engraving_generate_openai._call_openai_api_dual",
            return_value=dual_ret,
        ) as mock_d:
            result = generate_engraving_openai(
                str(self.project), self.json_path,
                mode=mode, force=force,
            )
            return result, mock_s, mock_d

    # ── silhouette mode ──────────────────────────────────────────────────────

    def test_silhouette_uses_single_image_call(self):
        _, mock_s, mock_d = self._run(mode="isolated")
        mock_s.assert_called_once()
        mock_d.assert_not_called()

    def test_silhouette_prompt_file_used(self):
        result, _, _ = self._run(mode="isolated")
        meta_text = result["metadata"].read_text()
        meta = json.loads(meta_text)
        self.assertIn("isolated", meta["prompt"]["prompt_file"])

    def test_silhouette_mode_stored_in_json(self):
        result, _, _ = self._run(mode="isolated")
        meta = json.loads(result["metadata"].read_text())
        self.assertEqual(meta["mode"], "isolated")

    def test_silhouette_engraving_png_has_mode_in_name(self):
        result, _, _ = self._run(mode="isolated")
        self.assertIn("isolated", result["engraving_png"].name)

    def test_silhouette_raw_png_written(self):
        result, _, _ = self._run(mode="isolated")
        self.assertTrue(result["raw_png"].exists())

    # ── full mode ────────────────────────────────────────────────────────────

    def test_full_uses_dual_image_call(self):
        _, mock_s, mock_d = self._run(mode="frame")
        mock_d.assert_called_once()
        mock_s.assert_not_called()

    def test_full_prompt_file_used(self):
        result, _, _ = self._run(mode="frame")
        meta = json.loads(result["metadata"].read_text())
        self.assertIn("frame", meta["prompt"]["prompt_file"])

    def test_full_mode_stored_in_json(self):
        result, _, _ = self._run(mode="frame")
        meta = json.loads(result["metadata"].read_text())
        self.assertEqual(meta["mode"], "frame")

    def test_full_engraving_png_has_mode_in_name(self):
        result, _, _ = self._run(mode="frame")
        self.assertIn("frame", result["engraving_png"].name)

    def test_full_inputs_include_source_frame(self):
        result, _, _ = self._run(mode="frame")
        meta = json.loads(result["metadata"].read_text())
        self.assertIsNotNone(meta.get("inputs", {}).get("source_frame_png"))

    # ── source frame re-rooting ──────────────────────────────────────────────

    def test_full_resolves_stale_absolute_path_via_media_anchor(self):
        """Re-roots a stale absolute path from another machine under project_path."""
        from services.engraving_generate_openai import _resolve_source_frame_png

        # Create a frame PNG under the current project
        frame_dir = self.project / "media" / "frames" / "best" / "movie" / "Django_1966"
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame_png = frame_dir / "shot_001.png"
        frame_png.write_bytes(_FAKE_PNG_BYTES)

        # Simulate a stale path from a different machine
        stale_path = "/home/other_user/other_project/media/frames/best/movie/Django_1966/shot_001.png"
        meta_with_stale = {**json.loads(self.json_path.read_text()), "source_frame": stale_path}

        resolved = _resolve_source_frame_png(
            self.json_path, meta_with_stale, str(self.project)
        )
        self.assertEqual(resolved, frame_png)

    # ── both mode ────────────────────────────────────────────────────────────

    def test_both_returns_list_of_two(self):
        result, _, _ = self._run(mode="both")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

    def test_both_first_result_is_silhouette(self):
        result, _, _ = self._run(mode="both")
        self.assertEqual(result[0]["mode"], "isolated")

    def test_both_second_result_is_full(self):
        result, _, _ = self._run(mode="both")
        self.assertEqual(result[1]["mode"], "frame")

    def test_both_calls_single_and_dual(self):
        _, mock_s, mock_d = self._run(mode="both")
        mock_s.assert_called_once()
        mock_d.assert_called_once()

    # ── force / skip ─────────────────────────────────────────────────────────

    def test_file_exists_error_without_force(self):
        from services.engraving_generate_openai import generate_engraving_openai
        # First generation
        with patch(
            "services.engraving_generate_openai._call_openai_api",
            return_value=_FAKE_PNG_BYTES,
        ):
            generate_engraving_openai(str(self.project), self.json_path, mode="isolated")
        # Second attempt without force
        with patch(
            "services.engraving_generate_openai._call_openai_api",
            return_value=_FAKE_PNG_BYTES,
        ):
            with self.assertRaises(FileExistsError):
                generate_engraving_openai(str(self.project), self.json_path, mode="isolated")

    def test_force_regenerates(self):
        from services.engraving_generate_openai import generate_engraving_openai
        with patch(
            "services.engraving_generate_openai._call_openai_api",
            return_value=_FAKE_PNG_BYTES,
        ):
            r1 = generate_engraving_openai(str(self.project), self.json_path, mode="isolated")
        # Overwrite raw.png with different content so we can detect the write
        r1["raw_png"].write_bytes(b"old")
        with patch(
            "services.engraving_generate_openai._call_openai_api",
            return_value=_FAKE_PNG_BYTES,
        ):
            r2 = generate_engraving_openai(
                str(self.project), self.json_path, mode="isolated", force=True
            )
        self.assertEqual(r2["raw_png"].read_bytes(), _FAKE_PNG_BYTES)

    def test_request_json_contains_mode(self):
        result, _, _ = self._run(mode="frame")
        request = json.loads(result["metadata"].parent.joinpath("request.json").read_text())
        self.assertEqual(request["mode"], "frame")


# ---------------------------------------------------------------------------
# scan_best_silhouettes
# ---------------------------------------------------------------------------

class TestScanBestSilhouettes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = _make_project(self.tmp)
        # Build a minimal silhouette catalog structure
        self.catalog_base = self.project / "data" / "silhouettes" / "catalog" / "movie"

    def tearDown(self):
        self._tmp.cleanup()

    def _sil_dir(self, film_stem: str, label: str) -> Path:
        d = self.catalog_base / film_stem / label
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_returns_only_human_best(self):
        from services.engraving_batch import scan_best_silhouettes

        d = self._sil_dir("django_1966", "horse")
        _make_silhouette(d, 1, human_best=True)
        _make_silhouette(d, 2, human_best=False)

        results = scan_best_silhouettes(str(self.project))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["meta"].get("human_best"))

    def test_empty_catalog_returns_empty_list(self):
        from services.engraving_batch import scan_best_silhouettes
        results = scan_best_silhouettes(str(self.project))
        self.assertEqual(results, [])

    def test_label_filter(self):
        from services.engraving_batch import scan_best_silhouettes

        d_horse = self._sil_dir("film_a", "horse")
        d_gun = self._sil_dir("film_a", "gun")
        _make_silhouette(d_horse, 1, human_best=True, label="horse")
        _make_silhouette(d_gun, 1, human_best=True, label="gun")

        results = scan_best_silhouettes(str(self.project), label="horse")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["meta"]["label"], "horse")

    def test_result_has_path_and_meta_keys(self):
        from services.engraving_batch import scan_best_silhouettes

        d = self._sil_dir("film_b", "horse")
        _make_silhouette(d, 1, human_best=True)

        results = scan_best_silhouettes(str(self.project))
        self.assertIn("path", results[0])
        self.assertIn("meta", results[0])


# ---------------------------------------------------------------------------
# batch_generate_engravings
# ---------------------------------------------------------------------------

class TestBatchGenerateEngravings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = _make_project(self.tmp)
        catalog_base = self.project / "data" / "silhouettes" / "catalog" / "movie"
        d = catalog_base / "django_1966" / "horse"
        d.mkdir(parents=True, exist_ok=True)
        frame_png = _make_source_frame_png(self.tmp)
        self.json_path = _make_silhouette(d, 1, human_best=True, source_frame=str(frame_png))
        self.targets = [{"path": self.json_path, "meta": json.loads(self.json_path.read_text()), "label": "horse"}]

    def tearDown(self):
        self._tmp.cleanup()

    def _patch_api(self):
        return patch(
            "services.engraving_generate_openai._call_openai_api",
            return_value=_FAKE_PNG_BYTES,
        ), patch(
            "services.engraving_generate_openai._call_openai_api_dual",
            return_value=_FAKE_PNG_BYTES,
        )

    def test_skips_already_generated(self):
        from services.engraving_batch import batch_generate_engravings
        # Pre-generate isolated
        paths = engraving_paths(str(self.project), self.json_path, self.targets[0]["meta"], "isolated")
        paths["dir"].mkdir(parents=True, exist_ok=True)
        paths["raw_png"].write_bytes(_FAKE_PNG_BYTES)

        p1, p2 = self._patch_api()
        with p1 as mock_s, p2:
            summary = batch_generate_engravings(
                str(self.project), self.targets, mode="isolated"
            )
        mock_s.assert_not_called()
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["generated"], 0)

    def test_force_regenerates_existing(self):
        from services.engraving_batch import batch_generate_engravings
        # Pre-generate isolated
        paths = engraving_paths(str(self.project), self.json_path, self.targets[0]["meta"], "isolated")
        paths["dir"].mkdir(parents=True, exist_ok=True)
        paths["raw_png"].write_bytes(b"old")

        p1, p2 = self._patch_api()
        with p1 as mock_s, p2:
            summary = batch_generate_engravings(
                str(self.project), self.targets, mode="isolated", force=True
            )
        mock_s.assert_called_once()
        self.assertEqual(summary["generated"], 1)
        self.assertEqual(summary["skipped"], 0)

    def test_generates_missing_silhouette(self):
        from services.engraving_batch import batch_generate_engravings
        p1, p2 = self._patch_api()
        with p1 as mock_s, p2:
            summary = batch_generate_engravings(
                str(self.project), self.targets, mode="isolated"
            )
        mock_s.assert_called_once()
        self.assertEqual(summary["generated"], 1)
        self.assertEqual(summary["failed"], 0)

    def test_both_mode_generates_two_passes(self):
        from services.engraving_batch import batch_generate_engravings
        p1, p2 = self._patch_api()
        with p1 as mock_s, p2 as mock_d:
            summary = batch_generate_engravings(
                str(self.project), self.targets, mode="both"
            )
        mock_s.assert_called_once()
        mock_d.assert_called_once()
        self.assertEqual(summary["generated"], 1)

    def test_failed_item_counted(self):
        from services.engraving_batch import batch_generate_engravings
        with patch(
            "services.engraving_generate_openai._call_openai_api",
            side_effect=RuntimeError("API error"),
        ), patch(
            "services.engraving_generate_openai._call_openai_api_dual",
            side_effect=RuntimeError("API error"),
        ):
            summary = batch_generate_engravings(
                str(self.project), self.targets, mode="isolated"
            )
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["generated"], 0)

    def test_on_item_done_called(self):
        from services.engraving_batch import batch_generate_engravings
        calls = []

        def _cb(target, result, exc):
            calls.append((target, exc))

        p1, p2 = self._patch_api()
        with p1, p2:
            batch_generate_engravings(
                str(self.project),
                self.targets,
                mode="isolated",
                on_item_done=_cb,
            )
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0][1])


if __name__ == "__main__":
    unittest.main()
