"""Tests for engraving benchmark candidate selection and shot evidence.

Two things these pin that are easy to regress:

- shot evidence must keep delegating frame-count and spacing decisions to
  ``data.annotate.canonical_context_frame_indices`` rather than growing a
  private ``shot_length / n`` sampler;
- benchmark work must never generate, publish or delete an engraving.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from services.engraving_benchmark import (
    CLEAN,
    DIFFICULT,
    MODERATE,
    STRUCTURAL_GROUPS,
    classify_structural_group,
    difficulty_stratum,
    mask_area_fraction,
    propose_candidates,
    resolve_benchmark_config,
    scale_stratum,
    selection_reasons,
)
from services.engraving_benchmark_evidence import (
    ROLE_ADAPTIVE,
    ROLE_BEST,
    ROLE_FIRST,
    ROLE_LAST,
    ShotEvidenceError,
    merge_roles,
    shot_evidence_for_record,
)

TITLE = "Companeros (1970) {tmdb-61044}"
SHOT = "tmdb_61044@f078383-f078476"


def _record(**overrides) -> dict:
    record = {
        "media_type": "movie",
        "filename": f"{TITLE}.mp4",
        "filename_stem": TITLE,
        "media_id": "tmdb_61044",
        "shot_id": SHOT,
        "frame": 78453,
        "label": "bird",
        "field": "animals",
        "object_id": "object_0002",
        "png": "object_0002.png",
        "mask_area": 248197,
        "frame_size": [1920, 816],
        "occlusion": "none",
        "completeness": "full",
        "fragment_count": 1,
        "touches_frame": False,
    }
    record.update(overrides)
    return record


class TestStructuralGrouping(unittest.TestCase):
    def test_known_labels_map_to_groups(self):
        cases = {
            ("animals", "horse"): "animal",
            ("objects", "chair"): "furniture",
            ("objects", "barrel"): "container",
            ("wearing", "coat"): "garment",
            ("objects", "rifle"): "weapon",
            ("objects", "wagon"): "vehicle",
            ("objects", "saddle"): "tack_harness",
            ("objects", "sign"): "signage",
            ("objects", "window"): "architecture",
            ("objects", "tree"): "vegetation",
            ("objects", "lamp"): "lighting_instrument",
            ("objects", "book"): "paper_media",
            ("objects", "curtain"): "textile_furnishing",
            ("setting", "mountain"): "environment",
        }
        for (field, label), expected in cases.items():
            self.assertEqual(classify_structural_group(field, label), expected, label)

    def test_colour_prefixed_labels_use_the_base_noun(self):
        self.assertEqual(classify_structural_group("wearing", "brown cowboy hat"), "garment")
        self.assertEqual(classify_structural_group("wearing", "black pants"), "garment")

    def test_verb_labels_are_not_classified(self):
        for label in ("ride", "galloping", "drinking", "clapping"):
            self.assertIsNone(classify_structural_group("action", label))

    def test_body_parts_survive_the_verb_field(self):
        self.assertEqual(classify_structural_group("action", "hand"), "human_body")
        self.assertEqual(classify_structural_group("action", "face"), "human_body")

    def test_unknown_labels_are_not_forced_into_a_bucket(self):
        self.assertIsNone(classify_structural_group("objects", "zzzz-unknown-thing"))

    def test_every_group_is_declared(self):
        for group in STRUCTURAL_LEXICON_GROUPS:
            self.assertIn(group, STRUCTURAL_GROUPS)


STRUCTURAL_LEXICON_GROUPS = STRUCTURAL_GROUPS


class TestStrata(unittest.TestCase):
    def test_clean_requires_all_three_signals(self):
        self.assertEqual(difficulty_stratum(_record()), CLEAN)

    def test_heavy_occlusion_with_cropping_is_difficult(self):
        self.assertEqual(
            difficulty_stratum(_record(occlusion="heavy", completeness="cropped")), DIFFICULT
        )

    def test_many_fragments_are_difficult(self):
        self.assertEqual(difficulty_stratum(_record(fragment_count=6)), DIFFICULT)

    def test_frame_edge_contact_is_difficult(self):
        self.assertEqual(difficulty_stratum(_record(touches_frame=True)), DIFFICULT)

    def test_mild_occlusion_is_moderate(self):
        self.assertEqual(difficulty_stratum(_record(occlusion="mild")), MODERATE)

    def test_mask_area_fraction_is_resolution_normalized(self):
        small_frame = _record(mask_area=1000, frame_size=[100, 100])
        large_frame = _record(mask_area=4000, frame_size=[200, 200])
        self.assertAlmostEqual(
            mask_area_fraction(small_frame), mask_area_fraction(large_frame)
        )

    def test_mask_area_fraction_handles_missing_metadata(self):
        self.assertIsNone(mask_area_fraction(_record(frame_size=None)))
        self.assertIsNone(mask_area_fraction(_record(mask_area=None)))
        self.assertIsNone(mask_area_fraction(_record(frame_size=[0, 0])))

    def test_scale_bands(self):
        self.assertEqual(scale_stratum(0.004), "small")
        self.assertEqual(scale_stratum(0.015), "medium")
        self.assertEqual(scale_stratum(0.30), "large")
        self.assertIsNone(scale_stratum(None))

    def test_selection_reasons_are_explicit(self):
        candidate = {
            "classification": {
                "structural_group": "animal",
                "difficulty_stratum": DIFFICULT,
                "scale_stratum": "large",
            },
            "evidence": {
                "occlusion": "heavy", "completeness": "cropped",
                "fragment_count": 1, "mask_area_fraction": 0.158, "touches_frame": False,
            },
        }
        reasons = selection_reasons(candidate)
        self.assertIn("structural_group = animal", reasons)
        self.assertIn("occlusion = heavy", reasons)


class TestShotEvidenceRoles(unittest.TestCase):
    def test_all_four_roles_are_present(self):
        merged = merge_roles(
            first=100, best=150, last=200, adaptive=[120, 180],
            shot_start=100, shot_end=200,
        )
        roles = {entry["frame"]: entry["roles"] for entry in merged}
        self.assertEqual(roles[100], [ROLE_FIRST])
        self.assertEqual(roles[150], [ROLE_BEST])
        self.assertEqual(roles[200], [ROLE_LAST])
        self.assertEqual(roles[120], [ROLE_ADAPTIVE])

    def test_duplicate_frames_collapse_but_keep_every_role(self):
        merged = merge_roles(
            first=100, best=150, last=200, adaptive=[100, 150],
            shot_start=100, shot_end=200,
        )
        self.assertEqual(len(merged), 3)
        roles = {entry["frame"]: entry["roles"] for entry in merged}
        self.assertEqual(roles[100], [ROLE_FIRST, ROLE_ADAPTIVE])
        self.assertEqual(roles[150], [ROLE_BEST, ROLE_ADAPTIVE])

    def test_frames_are_chronological(self):
        merged = merge_roles(
            first=100, best=150, last=200, adaptive=[190, 110],
            shot_start=100, shot_end=200,
        )
        self.assertEqual([e["frame"] for e in merged], sorted(e["frame"] for e in merged))

    def test_frames_outside_shot_bounds_are_dropped(self):
        merged = merge_roles(
            first=100, best=99, last=200, adaptive=[5, 500, 150],
            shot_start=100, shot_end=200,
        )
        frames = [entry["frame"] for entry in merged]
        self.assertEqual(frames, [100, 150, 200])
        self.assertTrue(all(100 <= f <= 200 for f in frames))

    def test_single_frame_shot_collapses_to_one_entry(self):
        merged = merge_roles(
            first=42, best=42, last=42, adaptive=[42], shot_start=42, shot_end=42,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0]["roles"], [ROLE_FIRST, ROLE_BEST, ROLE_LAST, ROLE_ADAPTIVE]
        )

    def test_missing_best_is_tolerated(self):
        merged = merge_roles(
            first=100, best=None, last=200, adaptive=[150],
            shot_start=100, shot_end=200,
        )
        self.assertEqual([e["frame"] for e in merged], [100, 150, 200])


class TestSamplerReuse(unittest.TestCase):
    """Architectural pin: benchmark evidence must not grow its own sampler."""

    def _project(self, tmp: str) -> Path:
        project = Path(tmp)
        shotlist = project / "data" / "shotlists" / "movie"
        shotlist.mkdir(parents=True)
        (shotlist / f"{TITLE}.csv").write_text(
            "Ignore,Scene,start_time,end_time,start_frame,end_frame,shot_id,Shot_Caption,Scene_Caption\n"
            f",,00:54:29.300,00:54:33.220,78383,78476,{SHOT},,\n",
            encoding="utf-8",
        )
        return project

    def test_evidence_delegates_to_the_canonical_sampler(self):
        with TemporaryDirectory() as tmp:
            project = self._project(tmp)
            with patch(
                "data.annotate.canonical_context_frame_indices",
                return_value=[78400, 78440],
            ) as sampler:
                evidence = shot_evidence_for_record(project, _record(), frames_per_shot=6)

            self.assertTrue(sampler.called, "must call the canonical shot sampler")
            self.assertEqual(
                evidence["sampler"]["source"],
                "data.annotate.canonical_context_frame_indices",
            )
            # The requested baseline is forwarded, not reimplemented.
            self.assertEqual(sampler.call_args.kwargs["frames_per_shot"], 6)
            self.assertEqual(evidence["adaptive"], [78400, 78440])

    def test_evidence_respects_shot_bounds_from_the_shot_id(self):
        with TemporaryDirectory() as tmp:
            project = self._project(tmp)
            with patch(
                "data.annotate.canonical_context_frame_indices",
                return_value=[1, 999999],
            ):
                evidence = shot_evidence_for_record(project, _record())
            for entry in evidence["selected_unique"]:
                self.assertGreaterEqual(entry["frame"], 78383)
                self.assertLessEqual(entry["frame"], 78476)

    def test_first_best_last_come_from_shot_id_and_record(self):
        with TemporaryDirectory() as tmp:
            project = self._project(tmp)
            with patch("data.annotate.canonical_context_frame_indices", return_value=[]):
                evidence = shot_evidence_for_record(project, _record())
            self.assertEqual(evidence["first"], 78383)
            self.assertEqual(evidence["best"], 78453)
            self.assertEqual(evidence["last"], 78476)

    def test_missing_shot_id_is_reported(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ShotEvidenceError):
                shot_evidence_for_record(Path(tmp), _record(shot_id=""))

    def test_unknown_shot_is_reported(self):
        with TemporaryDirectory() as tmp:
            project = self._project(tmp)
            with self.assertRaises(ShotEvidenceError):
                shot_evidence_for_record(project, _record(shot_id="tmdb_61044@f000001-f000002"))


def _index_project(tmp: str, records: list[dict]) -> Path:
    """Build a tiny silhouette index so selection can run without the corpus."""
    from services.illustration_index import rebuild_index

    project = Path(tmp)
    for record in records:
        directory = (
            project / "data" / "silhouettes" / "catalog" / "movie"
            / record["filename_stem"] / record["label"]
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{record['object_id']}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        Image.new("RGBA", (24, 24), (9, 9, 9, 255)).save(
            directory / f"{record['object_id']}.png"
        )
    rebuild_index(project, "silhouettes", "movie")
    return project


class TestCandidateSelection(unittest.TestCase):
    def _records(self) -> list[dict]:
        records = []
        for index, (label, field, film) in enumerate([
            ("horse", "animals", "Film A (1950) {tmdb-1}"),
            ("horse", "animals", "Film B (1951) {tmdb-2}"),
            ("horse", "animals", "Film C (1952) {tmdb-3}"),
            ("chair", "objects", "Film A (1950) {tmdb-1}"),
            ("barrel", "objects", "Film B (1951) {tmdb-2}"),
            ("rifle", "objects", "Film C (1952) {tmdb-3}"),
        ], start=1):
            records.append(_record(
                filename=f"{film}.mp4", filename_stem=film, label=label, field=field,
                object_id=f"object_{index:04d}", shot_id=f"tmdb_{index}@f000100-f000200",
                frame=150, mask_area=100000, frame_size=[1920, 1080],
            ))
        return records

    def test_selection_is_deterministic(self):
        with TemporaryDirectory() as tmp:
            project = _index_project(tmp, self._records())
            config = resolve_benchmark_config(None, {"target_candidates": 6, "reference_cases": []})
            first = propose_candidates(project, config)
            second = propose_candidates(project, config)
            self.assertEqual(
                [c["source"]["object_id"] for c in first["candidates"]],
                [c["source"]["object_id"] for c in second["candidates"]],
            )

    def test_per_label_cap_is_honoured(self):
        with TemporaryDirectory() as tmp:
            project = _index_project(tmp, self._records())
            config = resolve_benchmark_config(
                None, {"target_candidates": 10, "max_per_label": 1,
                       "max_per_film": 10, "reference_cases": []}
            )
            manifest = propose_candidates(project, config)
            labels = [c["source"]["label"] for c in manifest["candidates"]]
            self.assertEqual(len(labels), len(set(labels)))

    def test_per_film_cap_is_honoured(self):
        with TemporaryDirectory() as tmp:
            project = _index_project(tmp, self._records())
            config = resolve_benchmark_config(
                None, {"target_candidates": 10, "max_per_film": 1,
                       "max_per_label": 10, "reference_cases": []}
            )
            manifest = propose_candidates(project, config)
            films = [c["source"]["filename_stem"] for c in manifest["candidates"]]
            self.assertEqual(len(films), len(set(films)))

    def test_candidates_are_unique_canonical_objects(self):
        with TemporaryDirectory() as tmp:
            project = _index_project(tmp, self._records())
            config = resolve_benchmark_config(None, {"target_candidates": 10, "reference_cases": []})
            manifest = propose_candidates(project, config)
            identities = [
                (c["source"]["filename_stem"], c["source"]["label"], c["source"]["object_id"])
                for c in manifest["candidates"]
            ]
            self.assertEqual(len(identities), len(set(identities)))

    def test_candidates_start_as_proposed_with_reasons(self):
        with TemporaryDirectory() as tmp:
            project = _index_project(tmp, self._records())
            config = resolve_benchmark_config(None, {"target_candidates": 4, "reference_cases": []})
            manifest = propose_candidates(project, config)
            for candidate in manifest["candidates"]:
                self.assertEqual(candidate["review_state"], "proposed")
                self.assertTrue(candidate["selected_because"])
                self.assertIsNone(candidate["shot_evidence"])

    def test_tiny_objects_are_excluded(self):
        with TemporaryDirectory() as tmp:
            records = self._records()
            for record in records:
                record["mask_area"] = 10  # far below the minimum fraction
            project = _index_project(tmp, records)
            config = resolve_benchmark_config(None, {"reference_cases": []})
            self.assertEqual(propose_candidates(project, config)["candidate_count"], 0)

    def test_missing_index_is_reported_clearly(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                propose_candidates(Path(tmp), resolve_benchmark_config())


class TestRendering(unittest.TestCase):
    def test_review_pages_are_fixed_size_paginated_and_missing_safe(self):
        from services.engraving_benchmark_render import render_candidate_overview, _review_tile

        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            manifest = self._manifest(project, png=False)
            manifest["candidates"] *= 7
            outputs = render_candidate_overview(project, manifest, project / "page.png", per_sheet=6)
            self.assertEqual(len(outputs), 2)
            self.assertTrue(all(path.is_file() for path in outputs))
            self.assertEqual(_review_tile(project, manifest["candidates"][0], "caption", 560).size, (560, 710))

    def _manifest(self, project: Path, png: bool) -> dict:
        directory = project / "data" / "silhouettes" / "catalog" / "movie" / TITLE / "bird"
        directory.mkdir(parents=True, exist_ok=True)
        if png:
            Image.new("RGBA", (40, 40), (12, 12, 12, 255)).save(directory / "object_0002.png")
        return {
            "candidates": [{
                "benchmark_id": "bench-001",
                "source": {
                    "record_path": str((directory / "object_0002.json").relative_to(project)),
                    "png": "object_0002.png", "label": "bird", "field": "animals",
                    "filename_stem": TITLE, "frame": 78453,
                },
                "classification": {
                    "structural_group": "animal", "difficulty_stratum": DIFFICULT,
                    "scale_stratum": "large",
                },
                "evidence": {
                    "occlusion": "heavy", "completeness": "cropped",
                    "fragment_count": 1, "mask_area_fraction": 0.158,
                },
            }]
        }

    def test_overview_renders(self):
        from services.engraving_benchmark_render import render_candidate_overview

        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            manifest = self._manifest(project, png=True)
            output = project / "sheet.png"
            render_candidate_overview(project, manifest, output)
            self.assertTrue(output.is_file())

    def test_overview_handles_a_missing_image(self):
        from services.engraving_benchmark_render import render_candidate_overview

        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            manifest = self._manifest(project, png=False)
            output = project / "sheet.png"
            render_candidate_overview(project, manifest, output)
            self.assertTrue(output.is_file())

    def test_caption_reports_the_candidate_record(self):
        from services.engraving_benchmark_render import _candidate_caption

        with TemporaryDirectory() as tmp:
            candidate = self._manifest(Path(tmp), png=False)["candidates"][0]
            caption = _candidate_caption(candidate)
            self.assertIn("bench-001", caption)
            self.assertIn("animal", caption)
            self.assertIn("bird", caption)
            self.assertIn("heav", caption)

    def test_role_caption_shows_frame_and_roles(self):
        from services.engraving_benchmark_render import _role_caption

        caption = _role_caption({"frame": 78453, "roles": ["best", "adaptive"]}, 24.0)
        self.assertIn("f078453", caption)
        self.assertIn("BEST", caption)
        self.assertIn("ADAPTIVE", caption)


class TestReview(unittest.TestCase):
    def test_review_and_analysis_preserve_the_manifest_and_account_for_anchors(self):
        from services.engraving_benchmark_review import build_review, analyze_viability

        accepted = [f"bench-{index:03d}" for index in (1, 2, 4, 5, 12, 13, 15, 21, 22, 23, 24, 27, 32, 33, 34, 36)]
        manifest = {"candidates": [{
            "benchmark_id": f"bench-{index:03d}",
            "source": {"record_path": f"record-{index}.json", "label": "object"},
            "evidence": {"mask_area_fraction": .01},
        } for index in range(1, 41)]}
        before = json.dumps(manifest)
        review = build_review(manifest, accepted)
        self.assertEqual(review["counts"], {"accepted": 16, "rejected_visual_viability": 24})
        metrics = {candidate["source"]["record_path"]: TestViability()._metrics() for candidate in manifest["candidates"]}
        analysis = analyze_viability(manifest, review, metrics)
        self.assertEqual(analysis["confusion"], {"TP": 16, "FP": 24, "FN": 0, "TN": 0})
        self.assertEqual(analysis["accepted_anchors_lost"], [])
        self.assertEqual(json.dumps(manifest), before)


class TestViability(unittest.TestCase):
    def _metrics(self, short=100):
        return {
            "metrics_version": 2, "short_dimension_px": short,
            "mask_area_px": 10000, "largest_component_fraction": 1.0,
            "local_contrast": 20.0,
        }

    def test_policy_is_independent_from_image_viability(self):
        from services.engraving_benchmark import evaluate_proposal_policy, proposal_two_config
        from services.engraving_viability import evaluate_gate

        config = proposal_two_config()
        for group in STRUCTURAL_GROUPS:
            self.assertTrue(evaluate_gate(self._metrics(), {"structural_group": group})["viable"])
            self.assertTrue(evaluate_proposal_policy({"_group": group}, config)["eligible"])
        self.assertEqual(evaluate_proposal_policy({"_group": "weapon"}, config)["priority"], "fallback")

    def test_failed_candidates_cannot_fill_diversity_quotas(self):
        from services.engraving_benchmark import proposal_two_config

        with TemporaryDirectory() as tmp:
            project = _index_project(tmp, TestCandidateSelection()._records())
            config = proposal_two_config({"target_candidates": 6})
            with patch("services.engraving_viability.measure_silhouette", return_value=self._metrics(10)):
                manifest = propose_candidates(project, config)
            self.assertEqual(manifest["candidate_count"], 0)
            self.assertEqual(manifest["viability"]["gate_pass"], 0)

    def test_proposal_two_is_distinct_deterministic_and_cache_bounded(self):
        from services.engraving_benchmark import proposal_two_config

        with TemporaryDirectory() as tmp:
            project = _index_project(tmp, TestCandidateSelection()._records())
            config = proposal_two_config({"target_candidates": 6, "max_measurements": 20})
            with patch("services.engraving_viability.measure_silhouette", return_value=self._metrics()) as measure:
                first = propose_candidates(project, config)
                second = propose_candidates(project, {**config, "max_measurements": 0})
            self.assertEqual(first["candidates"], second["candidates"])
            self.assertTrue(first["candidates"])
            self.assertTrue(all(candidate["benchmark_id"].startswith("bench2-") for candidate in first["candidates"]))
            self.assertEqual(first["viability"]["measured"], measure.call_count)
            self.assertEqual(second["viability"]["measured"], 0)
            self.assertEqual(second["viability"]["cache_reused"], len(first["candidates"]))
            for dimension in ("label", "filename_stem"):
                from collections import Counter
                self.assertLessEqual(max(Counter(candidate["source"][dimension] for candidate in first["candidates"]).values()), 2)

    def test_reference_only_anchors_are_not_new_candidates_and_state_is_unchanged(self):
        from services.engraving_benchmark import proposal_two_config
        import hashlib

        with TemporaryDirectory() as tmp:
            project = _index_project(tmp, TestCandidateSelection()._records())
            records = list((project / "data/silhouettes/catalog").rglob("*.json"))
            config = proposal_two_config({"reference_only_paths": [str(path) for path in records]})
            bird = project / "data/engravings/catalog/bird/local/frame/run.json"
            bird.parent.mkdir(parents=True)
            bird.write_text('{"unchanged": true}')
            proposal_one = project / "outputs/tests/engraving-benchmark/candidates.json"
            proposal_one.parent.mkdir(parents=True)
            proposal_one.write_text('{"proposal": 1}')
            before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in project.rglob("*") if path.is_file()}
            with patch("services.engraving_local_pipeline.convert_one") as generate, \
                 patch("services.engraving_local_publish.publish_run") as publish, \
                 patch("services.engraving_lifecycle.apply_removal") as remove:
                result = propose_candidates(project, config)
            self.assertEqual(result["candidate_count"], 0)
            self.assertEqual(result["filtering_funnel"]["reference_only_excluded"], len(records))
            self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})
            generate.assert_not_called()
            publish.assert_not_called()
            remove.assert_not_called()

    def test_measurement_is_deterministic_and_safe_for_tiny_grayscale_and_color(self):
        from services.engraving_viability import measure_silhouette

        with TemporaryDirectory() as tmp:
            for mode in ("L", "RGB", "RGBA"):
                path = Path(tmp) / f"{mode}.png"
                Image.new(mode, (1, 1), 128).save(path)
                first = measure_silhouette(path)
                self.assertEqual(first, measure_silhouette(path))
                if mode != "RGBA":
                    self.assertEqual(first["mask_area_px"], 1)

    def test_bird_absolute_identity_is_reference_only_without_mutating_sources(self):
        from services.engraving_benchmark import proposal_two_config
        from services.engraving_benchmark_review import compare_proposals
        from services.engraving_viability import canonical_record_path

        with TemporaryDirectory() as tmp:
            project = _index_project(tmp, [_record()])
            relative = f"data/silhouettes/catalog/movie/{TITLE}/bird/object_0002.json"
            absolute = str(project / relative)
            self.assertEqual(canonical_record_path(project, absolute), relative)
            original = project / "outputs/tests/engraving-benchmark/candidates.json"
            original.parent.mkdir(parents=True)
            original.write_text(json.dumps({"benchmark_id": "bench-001", "record_path": absolute}))
            index = project / "data/indexes/illustration/movie-silhouettes.sqlite3"
            before = {path: path.read_bytes() for path in (original, index, project / relative)}
            proposal = propose_candidates(project, proposal_two_config({"reference_only_paths": [absolute]}))
            self.assertEqual(proposal["candidate_count"], 0)
            self.assertEqual(proposal["filtering_funnel"]["reference_only_excluded"], 1)
            analysis = {"cases": [{"benchmark_id": "bench-001", "label": "bird",
                                   "record_path": absolute, "decision": "accepted",
                                   "viability": {"viable": True}}]}
            comparison = compare_proposals(analysis, proposal, project)
            self.assertTrue(comparison["anchors"][0]["reference_only"])
            self.assertTrue(comparison["anchors"][0]["excluded_from_new_candidate_quota"])
            self.assertIsNone(comparison["anchors"][0]["proposal_2_id"])
            alias = {**proposal, "candidates": [{"benchmark_id": "bench2-001",
                                                 "source": {"record_path": relative}}]}
            self.assertEqual(compare_proposals(analysis, alias, project)["anchors"][0]["proposal_2_id"], "bench2-001")
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_gate_rejects_stale_metrics_with_an_explicit_reason(self):
        from services.engraving_viability import evaluate_gate

        verdict = evaluate_gate({"metrics_version": 1})
        self.assertFalse(verdict["viable"])
        self.assertEqual(verdict["reasons"], ["metrics_version_mismatch"])

    def test_cache_does_not_relabel_stale_metrics(self):
        from services.engraving_viability import store_cached_metrics

        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                store_cached_metrics(tmp, {"old.json": {"metrics_version": 1}})

    def test_cache_recognizes_absolute_and_relative_record_identity(self):
        from services.engraving_viability import store_cached_metrics, load_cached_metrics

        with TemporaryDirectory() as tmp:
            metrics = self._metrics()
            absolute = str(Path(tmp) / "data/silhouettes/object.json")
            store_cached_metrics(tmp, {absolute: metrics})
            self.assertEqual(load_cached_metrics(tmp, ["data/silhouettes/object.json"]),
                             {"data/silhouettes/object.json": metrics})

    def test_cache_only_returns_current_version(self):
        import sqlite3
        from services.engraving_viability import (
            METRICS_VERSION, cache_path, load_cached_metrics, store_cached_metrics,
        )

        with TemporaryDirectory() as tmp:
            current = {"metrics_version": METRICS_VERSION, "mask_area_px": 123}
            store_cached_metrics(tmp, {"current.json": current})
            with sqlite3.connect(cache_path(tmp)) as connection:
                connection.execute(
                    "INSERT INTO viability VALUES (?, ?, ?)",
                    ("old.json", 1, json.dumps({"metrics_version": 1})),
                )
            self.assertEqual(
                load_cached_metrics(tmp, ["old.json", "current.json"]),
                {"current.json": current},
            )


class TestSafety(unittest.TestCase):
    """Benchmark work must never generate, publish or delete an engraving."""

    def test_selection_does_not_touch_generation_or_publication(self):
        with TemporaryDirectory() as tmp:
            records = [_record(
                filename_stem="Film A (1950) {tmdb-1}", filename="Film A (1950) {tmdb-1}.mp4",
                label="horse", field="animals", object_id="object_0001",
                mask_area=100000, frame_size=[1920, 1080],
            )]
            project = _index_project(tmp, records)
            config = resolve_benchmark_config(None, {"reference_cases": []})

            with patch("services.engraving_local_pipeline.convert_one") as convert, \
                 patch("services.engraving_local_publish.publish_run") as publish, \
                 patch("services.engraving_lifecycle.apply_removal") as remove:
                propose_candidates(project, config)

            convert.assert_not_called()
            publish.assert_not_called()
            remove.assert_not_called()

    def test_selection_writes_nothing_into_the_catalog(self):
        with TemporaryDirectory() as tmp:
            records = [_record(
                filename_stem="Film A (1950) {tmdb-1}", filename="Film A (1950) {tmdb-1}.mp4",
                label="horse", field="animals", object_id="object_0001",
                mask_area=100000, frame_size=[1920, 1080],
            )]
            project = _index_project(tmp, records)
            catalog = project / "data" / "silhouettes" / "catalog"
            before = {p: p.stat().st_mtime for p in catalog.rglob("*") if p.is_file()}

            propose_candidates(project, resolve_benchmark_config(None, {"reference_cases": []}))

            after = {p: p.stat().st_mtime for p in catalog.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_engraving_catalog_is_never_created_by_selection(self):
        with TemporaryDirectory() as tmp:
            records = [_record(
                filename_stem="Film A (1950) {tmdb-1}", filename="Film A (1950) {tmdb-1}.mp4",
                label="horse", field="animals", object_id="object_0001",
                mask_area=100000, frame_size=[1920, 1080],
            )]
            project = _index_project(tmp, records)
            propose_candidates(project, resolve_benchmark_config(None, {"reference_cases": []}))
            self.assertFalse((project / "data" / "engravings").exists())


if __name__ == "__main__":
    unittest.main()
