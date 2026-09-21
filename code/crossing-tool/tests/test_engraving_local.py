"""Tests for the local (offline) engraving converter.

Covers the deterministic layers only — config resolution, evidence assembly,
path derivation, prompt construction, alpha extraction and the acceptance
gates.  Nothing here loads the 20B model; the stages that call it are exercised
through a stub.

Mirrors the ``unittest.TestCase`` style of the sibling engraving tests.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from services.engraving_local_alpha import (
    AlphaExtractionError,
    alpha_report,
    postprocess_file,
    white_to_alpha,
)
from services.engraving_local_config import (
    DEFAULT_CONFIG,
    EngravingConfigError,
    resolve_config,
    validate_config,
)
from services.engraving_local_evidence import (
    EvidenceError,
    assemble_evidence,
    assess_evidence,
    binary_mask,
    build_working_images,
    load_silhouette_record,
    resolve_silhouette_png,
)
from services.engraving_local_paths import (
    engraving_dir,
    engraving_paths,
    is_complete,
    output_filename,
)
from services.engraving_local_prompt import (
    EngravingPromptError,
    build_context,
    build_prompt,
    expand,
    load_template,
)
from services.engraving_local_validate import (
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    check_isolation,
    validate_result,
)


def _make_record(**overrides) -> dict:
    record = {
        "schema_version": "1",
        "media_type": "movie",
        "filename": "Companeros (1970) {tmdb-61044}.mp4",
        "filename_stem": "Companeros (1970) {tmdb-61044}",
        "media_id": "tmdb_61044",
        "shot_id": "tmdb_61044@f078383-f078476",
        "frame": 78453,
        "label": "bird",
        "field": "animals",
        "bbox": [424, 14, 495, 769],
        "mask_area": 248197,
        "frame_size": [1920, 816],
        "png": "object_0002.png",
        "touches_frame": False,
        "edge_touch": [],
        "fragment_count": 1,
        "completeness": "complete",
        "occlusion": "none",
    }
    record.update(overrides)
    return record


def _make_silhouette(path: Path, size=(200, 300)) -> None:
    """Write an RGBA silhouette with a solid opaque blob on transparency."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((40, 60, 160, 240), fill=(70, 60, 55, 255))
    image.save(path)


def _make_engraving(size=(200, 200), enclosed_white=True) -> Image.Image:
    """Ink ring on near-white, optionally enclosing a white interior."""
    image = Image.new("RGB", size, (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.ellipse((40, 40, 160, 160), outline=(10, 10, 10), width=6)
    if not enclosed_white:
        draw.ellipse((46, 46, 154, 154), fill=(10, 10, 10))
    return image


class TestConfig(unittest.TestCase):
    def test_default_config_is_valid(self):
        validate_config(DEFAULT_CONFIG)

    def test_resolve_applies_overrides(self):
        config = resolve_config(None, {"model": {"transformer_backend": "q4"}})
        self.assertEqual(config["model"]["transformer_backend"], "q4")
        # Untouched siblings survive the deep merge.
        self.assertEqual(
            config["evidence"]["working_size"], DEFAULT_CONFIG["evidence"]["working_size"]
        )

    def test_resolve_does_not_mutate_defaults(self):
        resolve_config(None, {"run": {"seed": 999}})
        self.assertEqual(DEFAULT_CONFIG["run"]["seed"], 42)

    def test_unknown_backend_rejected(self):
        with self.assertRaises(EngravingConfigError):
            resolve_config(None, {"model": {"transformer_backend": "int3"}})

    def test_working_size_must_be_multiple_of_16(self):
        with self.assertRaises(EngravingConfigError):
            resolve_config(None, {"evidence": {"working_size": 1000}})

    def test_both_stages_disabled_rejected(self):
        with self.assertRaises(EngravingConfigError):
            resolve_config(None, {"repair": {"enabled": False}, "engrave": {"enabled": False}})

    def test_config_file_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"run": {"seed": 7}}), encoding="utf-8")
            self.assertEqual(resolve_config(path)["run"]["seed"], 7)

    def test_missing_config_file_raises(self):
        with self.assertRaises(EngravingConfigError):
            resolve_config("/nonexistent/config.json")


class TestAlpha(unittest.TestCase):
    def test_enclosed_white_stays_opaque(self):
        result = white_to_alpha(_make_engraving())
        alpha = np.asarray(result.getchannel("A"))
        # Centre of the ring is enclosed white and must survive as opaque.
        self.assertEqual(int(alpha[alpha.shape[0] // 2, alpha.shape[1] // 2]), 255)

    def test_corners_are_transparent(self):
        result = white_to_alpha(_make_engraving())
        report = alpha_report(result)
        self.assertTrue(report["corners_transparent"])
        self.assertEqual(report["alpha_extrema"], [0, 255])

    def test_padding_is_added(self):
        padded = white_to_alpha(_make_engraving(), padding_fraction=0.10)
        tight = white_to_alpha(_make_engraving(), padding_fraction=0.0)
        self.assertGreater(padded.width, tight.width)

    def test_blank_page_raises(self):
        blank = Image.new("RGB", (64, 64), (255, 255, 255))
        with self.assertRaises(AlphaExtractionError):
            white_to_alpha(blank)

    def test_invalid_parameters_rejected(self):
        image = _make_engraving()
        with self.assertRaises(ValueError):
            white_to_alpha(image, white_point=0)
        with self.assertRaises(ValueError):
            white_to_alpha(image, padding_fraction=2.0)
        with self.assertRaises(ValueError):
            white_to_alpha(image, seal_size=4)

    def test_postprocess_file_writes_rgba(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "raw.png"
            dest = Path(tmp) / "nested" / "out.png"
            _make_engraving().save(src)
            report = postprocess_file(src, dest)
            self.assertTrue(dest.is_file())
            self.assertTrue(report["has_alpha"])
            with Image.open(dest) as opened:
                self.assertEqual(opened.mode, "RGBA")


class TestPaths(unittest.TestCase):
    def test_output_filename_format(self):
        name = output_filename("/x/object_0002.json", _make_record())
        self.assertEqual(name, "companeros-f078453-object_0002-local.png")

    def test_missing_frame_falls_back(self):
        name = output_filename("/x/object_0001.json", _make_record(frame=None))
        self.assertIn("f000000", name)

    def test_directory_layout_includes_local_mode(self):
        directory = engraving_dir("/proj", "/cat/bird/object_0002.json", _make_record())
        parts = directory.parts
        self.assertEqual(parts[-1], "local")
        self.assertEqual(parts[-2], "object_0002")
        self.assertEqual(parts[-3], "bird")
        self.assertIn("engravings", parts)

    def test_is_complete_requires_both_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = engraving_paths(tmp, "/cat/bird/object_0002.json", _make_record())
            paths["dir"].mkdir(parents=True, exist_ok=True)
            self.assertFalse(is_complete(paths))
            paths["run_json"].write_text("{}", encoding="utf-8")
            self.assertFalse(is_complete(paths))
            _make_engraving().save(paths["final_png"])
            self.assertTrue(is_complete(paths))


class TestPrompt(unittest.TestCase):
    def test_builtin_templates_load(self):
        for stage in ("repair", "engrave"):
            name, text = load_template("/nonexistent-project", stage)
            self.assertTrue(name.startswith("<built-in"))
            self.assertTrue(text)

    def test_unknown_stage_raises(self):
        with self.assertRaises(EngravingPromptError):
            load_template("/x", "sharpen")

    def test_project_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "prompts" / "engravings-local"
            directory.mkdir(parents=True)
            (directory / "repair-2026-01-v1.txt").write_text("older", encoding="utf-8")
            (directory / "repair-2026-02-v2.txt").write_text("NEWEST $label", encoding="utf-8")
            name, text = load_template(tmp, "repair")
            self.assertEqual(name, "repair-2026-02-v2.txt")
            self.assertEqual(text, "NEWEST $label")

    def test_expand_fills_and_preserves_unknown(self):
        self.assertEqual(expand("a $label b $nope", {"label": "horse"}), "a horse b $nope")

    def test_missing_variables_become_empty(self):
        self.assertEqual(expand("[$description]", {}), "[]")

    def test_context_uses_record_fields(self):
        evidence = {"record": _make_record(), "review": {"ambiguous": False}}
        context = build_context(evidence)
        self.assertEqual(context["label"], "bird")
        self.assertEqual(context["field"], "animals")
        self.assertEqual(context["movie"], "Companeros (1970) {tmdb-61044}")

    def test_prompt_mentions_label(self):
        evidence = {"record": _make_record(), "review": {"ambiguous": False}}
        result = build_prompt("/nonexistent-project", "repair", evidence)
        self.assertIn("bird", result["prompt"])
        self.assertEqual(result["stage"], "repair")

    def test_negative_prompt_blocks_invented_support(self):
        # The model reproduced the documented "invented perch" failure without
        # these terms; they must stay in the shipped defaults.
        for stage in ("repair", "engrave"):
            negative = DEFAULT_CONFIG[stage]["negative_prompt"]
            self.assertIn("perch", negative)
            self.assertIn("support", negative)
            self.assertIn("cast shadow", negative)


class TestEvidence(unittest.TestCase):
    def test_binary_mask_is_two_valued(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "s.png"
            _make_silhouette(png)
            with Image.open(png) as opened:
                mask = binary_mask(opened.convert("RGBA"))
            self.assertEqual(sorted(set(np.asarray(mask).ravel().tolist())), [0, 255])

    def test_mask_requires_alpha(self):
        with self.assertRaises(EvidenceError):
            binary_mask(Image.new("RGB", (8, 8), (0, 0, 0)))

    def test_working_images_are_square_and_record_transform(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "s.png"
            _make_silhouette(png)
            working = build_working_images(
                png, working_size=256, background_level=248, crop_padding_fraction=0.1
            )
            self.assertEqual(working["subject"].size, (256, 256))
            self.assertEqual(working["mask"].size, (256, 256))
            transform = working["transform"]
            self.assertEqual(transform["source_size"], [200, 300])
            self.assertEqual(transform["working_size"], [256, 256])
            self.assertIn("scale", transform)
            self.assertIn("paste_offset", transform)

    def test_empty_mask_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "empty.png"
            Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(png)
            with self.assertRaises(EvidenceError):
                build_working_images(
                    png, working_size=64, background_level=248, crop_padding_fraction=0.0
                )

    def test_clean_record_is_not_ambiguous(self):
        review = assess_evidence(_make_record(), DEFAULT_CONFIG)
        self.assertFalse(review["ambiguous"])
        self.assertEqual(review["reasons"], [])

    def test_heavy_occlusion_flags_review(self):
        review = assess_evidence(_make_record(occlusion="heavy"), DEFAULT_CONFIG)
        self.assertTrue(review["ambiguous"])
        self.assertTrue(any("occlusion" in r for r in review["reasons"]))

    def test_cropped_completeness_flags_review(self):
        review = assess_evidence(_make_record(completeness="cropped"), DEFAULT_CONFIG)
        self.assertTrue(review["ambiguous"])

    def test_fragmented_mask_flags_review(self):
        review = assess_evidence(_make_record(fragment_count=5), DEFAULT_CONFIG)
        self.assertTrue(review["ambiguous"])
        self.assertTrue(any("fragment" in r for r in review["reasons"]))

    def test_frame_edge_touch_flags_review(self):
        record = _make_record(touches_frame=True, edge_touch=["left"])
        review = assess_evidence(record, DEFAULT_CONFIG)
        self.assertTrue(review["ambiguous"])
        self.assertTrue(any("edge" in r for r in review["reasons"]))

    def test_assemble_reads_sibling_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "object_0002.json"
            record = _make_record()
            source.write_text(json.dumps(record), encoding="utf-8")
            _make_silhouette(Path(tmp) / "object_0002.png")

            config = resolve_config(None, {"evidence": {"working_size": 256}})
            evidence = assemble_evidence(source, config)
            self.assertEqual(evidence["subject"].size, (256, 256))
            self.assertIsNone(evidence["source_frame"])
            self.assertEqual(evidence["provenance"]["label"], "bird")

    def test_missing_png_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "object_0002.json"
            source.write_text(json.dumps(_make_record()), encoding="utf-8")
            with self.assertRaises(EvidenceError):
                resolve_silhouette_png(source, _make_record())

    def test_invalid_record_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad.json"
            source.write_text("{not json", encoding="utf-8")
            with self.assertRaises(EvidenceError):
                load_silhouette_record(source)


class TestValidation(unittest.TestCase):
    def _evidence(self):
        mask = Image.new("L", (256, 256), 0)
        ImageDraw.Draw(mask).ellipse((60, 60, 196, 196), fill=255)
        return {"mask": mask, "review": {"ambiguous": False, "reasons": []}}

    def test_good_result_needs_review_never_accepted(self):
        rgba = white_to_alpha(_make_engraving(enclosed_white=False))
        verdict = validate_result(rgba, self._evidence(), alpha_report(rgba), DEFAULT_CONFIG)
        self.assertEqual(verdict["status"], STATUS_NEEDS_REVIEW)
        self.assertTrue(verdict["manual_review_required"])
        self.assertNotIn("accepted", verdict["status"])

    def test_sparse_ink_is_rejected(self):
        image = Image.new("RGB", (400, 400), (250, 250, 250))
        ImageDraw.Draw(image).rectangle((198, 198, 202, 202), fill=(0, 0, 0))
        rgba = white_to_alpha(image)
        config = resolve_config(None, {"validate": {"min_ink_fraction": 0.90}})
        verdict = validate_result(rgba, self._evidence(), alpha_report(rgba), config)
        self.assertEqual(verdict["status"], STATUS_REJECTED)
        self.assertTrue(verdict["automatic_failures"])

    def test_scattered_debris_fails_isolation(self):
        image = Image.new("RGB", (400, 400), (250, 250, 250))
        draw = ImageDraw.Draw(image)
        for x in range(0, 400, 45):
            for y in range(0, 400, 45):
                draw.ellipse((x + 6, y + 6, x + 26, y + 26), fill=(0, 0, 0))
        rgba = white_to_alpha(image)
        gate = check_isolation(rgba, DEFAULT_CONFIG)
        self.assertFalse(gate["passed"])
        self.assertGreater(gate["component_count"], DEFAULT_CONFIG["validate"]["max_ink_components"])

    def test_evidence_reasons_are_carried_into_verdict(self):
        evidence = self._evidence()
        evidence["review"] = {"ambiguous": True, "reasons": ["source object is cropped"]}
        rgba = white_to_alpha(_make_engraving(enclosed_white=False))
        verdict = validate_result(rgba, evidence, alpha_report(rgba), DEFAULT_CONFIG)
        self.assertEqual(verdict["evidence_review_reasons"], ["source object is cropped"])

    def test_manual_gates_are_reported(self):
        rgba = white_to_alpha(_make_engraving(enclosed_white=False))
        verdict = validate_result(rgba, self._evidence(), alpha_report(rgba), DEFAULT_CONFIG)
        self.assertIn("exact_part_count_and_connectivity", verdict["manual_gates"])
        self.assertIn("no_unsupported_additions", verdict["manual_gates"])


if __name__ == "__main__":
    unittest.main()
