"""Tests for engraving publication and lifecycle management.

Covers the boundary that matters once local engravings became real
Illustrations: a generated run publishes exactly one canonical asset, its
intermediates never gain an Illustration identity, removal is reference-aware
and dry-run by default, and the doctor detects the common orphan/dangling
conditions.

Fixtures are small on-disk projects rather than full integration environments;
the index is a derived artifact so it can be rebuilt inside a tmp_path.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from services.engraving_lifecycle import (
    BLOCKING,
    REPAIRABLE,
    WARNING,
    apply_removal,
    book_references,
    doctor,
    orphan_provenance_dirs,
    plan_removal,
    scan_engravings,
)
from services.engraving_local_publish import (
    GENERATION_SERVICE,
    PublicationError,
    build_engraving_metadata,
    canonical_mode,
    set_review_state,
)
from services.illustration_index import query_page, rebuild_index

TITLE = "Companeros (1970) {tmdb-61044}"


def _png(path: Path, size=(32, 32)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, (10, 10, 10, 255)).save(path)


def _engraving(
    project: Path,
    *,
    label: str = "bird",
    object_id: str = "object_0002",
    mode: str = "frame",
    status: str = "generated",
    backend: str = GENERATION_SERVICE,
    review_state: str = "needs_review",
    title: str = TITLE,
    with_png: bool = True,
) -> Path:
    """Create one canonical engraving on disk and return its engraving.json."""
    mode_dir = (
        project / "data" / "engravings" / "catalog" / "movie"
        / title / label / object_id / mode
    )
    mode_dir.mkdir(parents=True, exist_ok=True)
    png_name = f"stub-f000001-{object_id}-{mode}.png"
    if with_png:
        _png(mode_dir / png_name)

    metadata = {
        "schema_version": "2",
        "status": status,
        "mode": mode,
        "review_state": review_state,
        "generation_service": backend,
        "silhouette": {
            "label": label,
            "field": "animals",
            "media_type": "movie",
            "filename_stem": title,
            "media_id": "tmdb_61044",
            "shot_id": "tmdb_61044@f078383-f078476",
            "frame": 78453,
        },
        "source": {
            "silhouette_json": f"data/silhouettes/catalog/movie/{title}/{label}/{object_id}.json",
        },
        "generation": {"service": backend, "model": "stub"},
        "output_png": str(
            (mode_dir / png_name).relative_to(project)
        ),
    }
    path = mode_dir / "engraving.json"
    path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return path


def _silhouette(project: Path, label: str = "bird", object_id: str = "object_0002") -> None:
    directory = project / "data" / "silhouettes" / "catalog" / "movie" / TITLE / label
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{object_id}.json").write_text(json.dumps({"label": label}), encoding="utf-8")


def _book(project: Path, slug: str, layers: list[dict]) -> Path:
    book_dir = project / "outputs" / "books" / slug
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "layers.json").write_text(json.dumps(layers), encoding="utf-8")
    return book_dir


def _run_record(project: Path, mode_dir: Path) -> dict:
    """Build a minimal completed local run record."""
    run_dir = mode_dir.parent / "local"
    run_dir.mkdir(parents=True, exist_ok=True)
    final = run_dir / "03-engraving-alpha.png"
    _png(final)
    _png(run_dir / "01-repair.png")
    run_json = run_dir / "run.json"
    record = {
        "status": "needs_review",
        "seed": 42,
        "source_json": str(
            project / "data" / "silhouettes" / "catalog" / "movie" / TITLE / "bird" / "object_0002.json"
        ),
        "run_json": str(run_json),
        "provenance": {
            "label": "bird", "field": "animals", "media_type": "movie",
            "filename": f"{TITLE}.mp4", "filename_stem": TITLE,
            "media_id": "tmdb_61044", "shot_id": "tmdb_61044@f078383-f078476",
            "frame": 78453,
        },
        "inputs": {
            "silhouette_png": str(project / "data" / "silhouettes" / "catalog" / "movie" / TITLE / "bird" / "object_0002.png"),
            "source_frame": None,
            "context_frames": [],
        },
        "model": {
            "pipeline_repo": "Qwen/Qwen-Image-Edit-2511",
            "pipeline_license": "apache-2.0",
            "transformer_backend": "q8",
        },
        "stages": [{"stage": "repair"}, {"stage": "engrave"}, {"stage": "postprocess"}],
        "validation": {"status": "needs_review"},
        "outputs": {"final_png": str(final)},
        "total_seconds": 1.0,
        "peak_vram_bytes": 1,
    }
    run_json.write_text(json.dumps(record), encoding="utf-8")
    return record


class TestCanonicalMode(unittest.TestCase):
    def test_source_frame_maps_to_frame_mode(self):
        self.assertEqual(canonical_mode({"evidence": {"use_source_frame": True}}), "frame")

    def test_no_source_frame_maps_to_isolated_mode(self):
        self.assertEqual(canonical_mode({"evidence": {"use_source_frame": False}}), "isolated")

    def test_backend_is_never_a_mode(self):
        for config in ({"evidence": {"use_source_frame": True}}, {"evidence": {}}):
            self.assertIn(canonical_mode(config), ("isolated", "frame"))


class TestPublication(unittest.TestCase):
    def test_metadata_carries_scalar_backend_and_review_state(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            run = _run_record(project, eng.parent)
            metadata = build_engraving_metadata(
                project, run, mode="frame",
                output_png=eng.parent / "out.png", review_state="needs_review",
            )
            # Scalars survive into the index payload; dicts do not.
            self.assertEqual(metadata["generation_service"], GENERATION_SERVICE)
            self.assertEqual(metadata["review_state"], "needs_review")
            self.assertEqual(metadata["status"], "generated")
            self.assertEqual(metadata["generation"]["service"], GENERATION_SERVICE)

    def test_metadata_records_provenance_and_source_object(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            run = _run_record(project, eng.parent)
            metadata = build_engraving_metadata(
                project, run, mode="frame",
                output_png=eng.parent / "out.png", review_state="needs_review",
            )
            self.assertIn("run.json", metadata["provenance"]["run_json"])
            self.assertEqual(metadata["provenance"]["validation_status"], "needs_review")
            self.assertEqual(metadata["silhouette"]["shot_id"], "tmdb_61044@f078383-f078476")
            self.assertEqual(metadata["generation"]["seed"], 42)

    def test_evidence_frames_list_is_forward_compatible(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            run = _run_record(project, eng.parent)
            run["inputs"]["source_frame"] = str(project / "f1.png")
            run["inputs"]["context_frames"] = [str(project / "f2.png")]
            metadata = build_engraving_metadata(
                project, run, mode="frame",
                output_png=eng.parent / "out.png", review_state="needs_review",
            )
            self.assertEqual(len(metadata["source"]["evidence_frames"]), 2)

    def test_published_engraving_is_indexed_once(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _engraving(project)
            _run_record(project, project / "data" / "engravings" / "catalog" / "movie" / TITLE / "bird" / "object_0002" / "frame")
            rebuild_index(project, "engravings", "movie")
            page = query_page(project, "engravings", "movie", limit=50)
            self.assertEqual(page["total"], 1)

    def test_intermediates_do_not_become_illustrations(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            # Run artifacts sit beside the canonical mode dir with no
            # engraving.json, so they must never be indexed.
            _run_record(project, eng.parent)
            rebuild_index(project, "engravings", "movie")
            page = query_page(project, "engravings", "movie", limit=50)
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["records"][0]["mode"], "frame")

    def test_review_state_is_queryable_from_index(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _engraving(project, review_state="needs_review")
            rebuild_index(project, "engravings", "movie")
            page = query_page(project, "engravings", "movie", limit=5)
            self.assertEqual(page["records"][0]["review_state"], "needs_review")
            self.assertEqual(page["records"][0]["generation_service"], GENERATION_SERVICE)

    def test_rejected_review_removes_record_without_deleting_asset(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            png = eng.parent / "stub-f000001-object_0002-frame.png"
            set_review_state(eng, "rejected")
            rebuild_index(project, "engravings", "movie")
            self.assertEqual(query_page(project, "engravings", "movie", limit=5)["total"], 0)
            self.assertTrue(png.is_file())

    def test_accept_restores_generated_status(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            set_review_state(eng, "rejected")
            metadata = set_review_state(eng, "accepted")
            self.assertEqual(metadata["status"], "generated")
            self.assertEqual(metadata["review_state"], "accepted")

    def test_unknown_review_state_rejected(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            with self.assertRaises(PublicationError):
                set_review_state(eng, "looks-nice")


class TestScanAndSelection(unittest.TestCase):
    def test_scan_reports_identity_fields(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _engraving(project)
            entries = scan_engravings(project, media_type="movie")
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["label"], "bird")
            self.assertEqual(entry["object_id"], "object_0002")
            self.assertEqual(entry["mode"], "frame")
            self.assertEqual(entry["title"], TITLE)
            self.assertEqual(entry["backend"], GENERATION_SERVICE)

    def test_scan_filters_by_backend_and_label(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _engraving(project, label="bird", backend=GENERATION_SERVICE)
            _engraving(project, label="coffin", object_id="object_0009",
                       mode="isolated", backend="openai")
            self.assertEqual(len(scan_engravings(project, backend="openai")), 1)
            self.assertEqual(len(scan_engravings(project, label="bird")), 1)
            self.assertEqual(len(scan_engravings(project)), 2)

    def test_unreadable_metadata_is_reported_not_skipped(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            eng.write_text("{not json", encoding="utf-8")
            entries = scan_engravings(project)
            self.assertTrue(entries[0]["unreadable"])


class TestRemoval(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            png = eng.parent / "stub-f000001-object_0002-frame.png"
            plan = plan_removal(project, scan_engravings(project))
            self.assertTrue(plan["safe"])
            self.assertEqual(plan["selected"], 1)
            # Planning must not touch the filesystem.
            self.assertTrue(eng.is_file())
            self.assertTrue(png.is_file())

    def test_apply_removes_exactly_the_selection(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            target = _engraving(project, label="bird")
            other = _engraving(project, label="coffin", object_id="object_0009",
                               mode="isolated", backend="openai")
            plan = plan_removal(project, scan_engravings(project, label="bird"))
            apply_removal(project, plan)
            self.assertFalse(target.is_file())
            self.assertTrue(other.is_file())

    def test_apply_leaves_provenance_by_default(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            _run_record(project, eng.parent)
            run_dir = eng.parent.parent / "local"
            plan = plan_removal(project, scan_engravings(project))
            apply_removal(project, plan)
            self.assertFalse(eng.is_file())
            self.assertTrue((run_dir / "run.json").is_file())

    def test_include_provenance_removes_run_directory(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            _run_record(project, eng.parent)
            run_dir = eng.parent.parent / "local"
            plan = plan_removal(project, scan_engravings(project), include_provenance=True)
            self.assertTrue(plan["provenance_roots"])
            apply_removal(project, plan)
            self.assertFalse(run_dir.exists())

    def test_missing_canonical_file_is_a_warning_not_a_crash(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _engraving(project, with_png=False)
            plan = plan_removal(project, scan_engravings(project))
            self.assertTrue(plan["warnings"])
            self.assertTrue(plan["safe"])
            apply_removal(project, plan)

    def test_repeated_removal_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _engraving(project)
            plan = plan_removal(project, scan_engravings(project))
            apply_removal(project, plan)
            self.assertEqual(scan_engravings(project), [])
            # A second pass selects nothing and must not raise.
            second = plan_removal(project, scan_engravings(project))
            result = apply_removal(project, second)
            self.assertEqual(result["removed_files"], 0)

    def test_unreadable_metadata_blocks_removal(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            eng.write_text("{not json", encoding="utf-8")
            plan = plan_removal(project, scan_engravings(project))
            self.assertFalse(plan["safe"])
            with self.assertRaises(RuntimeError):
                apply_removal(project, plan)

    def test_removal_invalidates_but_never_rebuilds_the_index(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _engraving(project)
            rebuild_index(project, "engravings", "movie")
            plan = plan_removal(project, scan_engravings(project))
            result = apply_removal(project, plan)
            self.assertEqual(result["invalidated"], ["movie"])
            # The stale index must still exist; rebuilding is a separate command.
            db = project / "data" / "indexes" / "illustration" / "movie-engravings.sqlite3"
            self.assertTrue(db.is_file())

    def test_plan_counts_index_records_despite_display_title_rewriting(self):
        # The index stores a display title with the "{tmdb-...}" suffix
        # stripped, so the plan must match on the record path instead.
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _engraving(project)
            rebuild_index(project, "engravings", "movie")
            plan = plan_removal(project, scan_engravings(project))
            self.assertEqual(plan["index_records"]["matched"], 1)
            self.assertEqual(plan["index_records"]["index_status"]["movie"], "ready")


class TestBookCompatibility(unittest.TestCase):
    def test_self_contained_book_copies_are_not_external(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            book_dir = _book(project, "west", [
                {"id": "img_1", "type": "Image", "source": "illustrations/silhouettes/a.png"},
            ])
            _png(book_dir / "illustrations" / "silhouettes" / "a.png")
            refs = book_references(project)
            self.assertEqual(len(refs["self_contained"]), 1)
            self.assertEqual(refs["external_references"], [])
            self.assertEqual(refs["missing_references"], [])

    def test_absolute_book_reference_is_detected_as_external(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            png = eng.parent / "stub-f000001-object_0002-frame.png"
            _book(project, "fragile", [
                {"id": "img_1", "type": "Image", "output_png": str(png)},
            ])
            refs = book_references(project)
            self.assertEqual(len(refs["external_references"]), 1)
            self.assertEqual(refs["external_references"][0]["key"], "output_png")

    def test_dry_run_detects_book_dependency_and_blocks_removal(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            png = eng.parent / "stub-f000001-object_0002-frame.png"
            _book(project, "fragile", [
                {"id": "img_1", "type": "Image", "output_png": str(png)},
            ])
            plan = plan_removal(project, scan_engravings(project))
            self.assertTrue(plan["book_dependencies"])
            self.assertFalse(plan["safe"])
            with self.assertRaises(RuntimeError):
                apply_removal(project, plan)
            # Nothing removed while the book still points at it.
            self.assertTrue(png.is_file())

    def test_removal_proceeds_when_no_book_depends_on_the_asset(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            book_dir = _book(project, "safe", [
                {"id": "img_1", "type": "Image", "source": "illustrations/silhouettes/a.png"},
            ])
            _png(book_dir / "illustrations" / "silhouettes" / "a.png")
            plan = plan_removal(project, scan_engravings(project))
            self.assertTrue(plan["safe"])
            apply_removal(project, plan)
            self.assertFalse(eng.is_file())
            # The book's own copy is untouched.
            self.assertTrue((book_dir / "illustrations" / "silhouettes" / "a.png").is_file())


class TestDoctor(unittest.TestCase):
    def _checks(self, report):
        return {finding["check"] for finding in report["findings"]}

    def test_clean_installation_reports_no_findings(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            _engraving(project, backend="openai")
            rebuild_index(project, "engravings", "movie")
            report = doctor(project, media_type="movie")
            self.assertEqual(report["findings"], [])
            self.assertEqual(report["status"], "ok")

    def test_missing_canonical_file_is_detected(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            _engraving(project, with_png=False)
            rebuild_index(project, "engravings", "movie")
            self.assertIn("missing_canonical_asset", self._checks(doctor(project, media_type="movie")))

    def test_stale_index_record_is_detected(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            eng = _engraving(project)
            rebuild_index(project, "engravings", "movie")
            eng.unlink()
            report = doctor(project, media_type="movie")
            self.assertIn("stale_index_record", self._checks(report))

    def test_unindexed_asset_is_detected(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            _engraving(project)
            rebuild_index(project, "engravings", "movie")
            _silhouette(project, label="coffin", object_id="object_0009")
            _engraving(project, label="coffin", object_id="object_0009", mode="isolated")
            self.assertIn("unindexed_asset", self._checks(doctor(project, media_type="movie")))

    def test_orphaned_generation_directory_is_detected(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            object_dir = (
                project / "data" / "engravings" / "catalog" / "movie"
                / TITLE / "bird" / "object_0002"
            )
            run_dir = object_dir / "local"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text("{}", encoding="utf-8")
            self.assertEqual(len(orphan_provenance_dirs(project, "movie")), 1)
            self.assertIn("unpublished_run", self._checks(doctor(project, media_type="movie")))

    def test_published_run_is_not_reported_as_orphan(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            eng = _engraving(project)
            _run_record(project, eng.parent)
            self.assertEqual(orphan_provenance_dirs(project, "movie"), [])

    def test_dangling_book_reference_is_detected(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _book(project, "broken", [
                {"id": "img_1", "type": "Image", "source": "illustrations/gone.png"},
            ])
            report = doctor(project, media_type="movie")
            self.assertIn("dangling_book_reference", self._checks(report))
            self.assertEqual(report["status"], BLOCKING)

    def test_provenance_missing_source_object_is_detected(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _engraving(project)  # no silhouette written
            rebuild_index(project, "engravings", "movie")
            self.assertIn("provenance_missing_source", self._checks(doctor(project, media_type="movie")))

    def test_local_engraving_without_provenance_is_flagged(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            _engraving(project, backend=GENERATION_SERVICE)
            rebuild_index(project, "engravings", "movie")
            report = doctor(project, media_type="movie")
            self.assertIn("missing_provenance", self._checks(report))
            self.assertEqual(report["status"], WARNING)

    def test_duplicate_identity_is_blocking(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            _engraving(project, mode="frame")
            # An "isolated" directory whose metadata claims mode "frame"
            # collides with the real frame engraving on identity.
            colliding = _engraving(project, mode="isolated")
            metadata = json.loads(colliding.read_text(encoding="utf-8"))
            metadata["mode"] = "frame"
            colliding.write_text(json.dumps(metadata), encoding="utf-8")

            report = doctor(project, media_type="movie")
            self.assertIn("duplicate_illustration", self._checks(report))
            self.assertEqual(report["status"], BLOCKING)

    def test_repairable_index_state_is_not_blocking(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            _engraving(project, backend="openai")
            # Index never built at all.
            report = doctor(project, media_type="movie")
            self.assertIn("missing_index", self._checks(report))
            self.assertEqual(report["status"], REPAIRABLE)


if __name__ == "__main__":
    unittest.main()
