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
    provenance_dirs_for,
    scan_engravings,
)
from services.engraving_local_paths import (
    find_legacy_runs,
    migrate_legacy_run,
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


def _run_record(project: Path, mode_dir: Path, *, mode: str | None = None, legacy: bool = False) -> dict:
    """Build a minimal completed local run record.

    Writes the mode-aware provenance layout by default; *legacy* reproduces the
    pre-mode-aware ``…/local/run.json`` directory.
    """
    mode = mode or mode_dir.name
    local_root = mode_dir.parent / "local"
    run_dir = local_root if legacy else local_root / mode
    run_dir.mkdir(parents=True, exist_ok=True)
    final = run_dir / "03-engraving-alpha.png"
    _png(final)
    _png(run_dir / "01-repair.png")
    run_json = run_dir / "run.json"
    record = {
        "status": "needs_review",
        "seed": 42,
        "mode": mode,
        "config": {"evidence": {"use_source_frame": mode == "frame"}},
        "publication": {"published": True, "mode": mode},
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
            run_dir = eng.parent.parent / "local" / "frame"
            plan = plan_removal(project, scan_engravings(project))
            apply_removal(project, plan)
            self.assertFalse(eng.is_file())
            self.assertTrue((run_dir / "run.json").is_file())

    def test_include_provenance_removes_run_directory(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng = _engraving(project)
            _run_record(project, eng.parent)
            run_dir = eng.parent.parent / "local" / "frame"
            plan = plan_removal(project, scan_engravings(project), include_provenance=True)
            self.assertEqual(plan["provenance_roots"], [run_dir])
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


class TestModeAwareProvenance(unittest.TestCase):
    """(object, mode) must own an independent provenance namespace."""

    def _both_modes(self, project: Path):
        isolated = _engraving(project, mode="isolated")
        frame = _engraving(project, mode="frame")
        _run_record(project, isolated.parent, mode="isolated")
        _run_record(project, frame.parent, mode="frame")
        return isolated, frame

    def test_both_modes_coexist_on_disk(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            isolated, frame = self._both_modes(project)
            local = isolated.parent.parent / "local"
            self.assertTrue((local / "isolated" / "run.json").is_file())
            self.assertTrue((local / "frame" / "run.json").is_file())
            self.assertTrue(isolated.is_file())
            self.assertTrue(frame.is_file())

    def test_each_mode_has_independent_intermediates(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            isolated, _ = self._both_modes(project)
            local = isolated.parent.parent / "local"
            for mode in ("isolated", "frame"):
                self.assertTrue((local / mode / "01-repair.png").is_file())
                self.assertTrue((local / mode / "03-engraving-alpha.png").is_file())
            run = json.loads((local / "frame" / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["mode"], "frame")

    def test_both_modes_publish_as_distinct_illustrations(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._both_modes(project)
            rebuild_index(project, "engravings", "movie")
            page = query_page(project, "engravings", "movie", limit=10)
            self.assertEqual(page["total"], 2)
            self.assertEqual(
                sorted(r["mode"] for r in page["records"]), ["frame", "isolated"]
            )

    def test_provenance_selection_is_mode_scoped(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            isolated, _ = self._both_modes(project)
            local = isolated.parent.parent / "local"
            entries = scan_engravings(project, mode="isolated")
            self.assertEqual(len(entries), 1)
            self.assertEqual(provenance_dirs_for(entries[0]), [local / "isolated"])

    def test_removing_isolated_keeps_frame_provenance(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            isolated, frame = self._both_modes(project)
            local = isolated.parent.parent / "local"
            plan = plan_removal(
                project, scan_engravings(project, mode="isolated"), include_provenance=True
            )
            self.assertEqual(plan["provenance_roots"], [local / "isolated"])
            apply_removal(project, plan)
            self.assertFalse((local / "isolated").exists())
            self.assertTrue((local / "frame" / "run.json").is_file())
            self.assertTrue(frame.is_file())

    def test_removing_frame_keeps_isolated_provenance(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            isolated, _ = self._both_modes(project)
            local = isolated.parent.parent / "local"
            plan = plan_removal(
                project, scan_engravings(project, mode="frame"), include_provenance=True
            )
            self.assertEqual(plan["provenance_roots"], [local / "frame"])
            apply_removal(project, plan)
            self.assertFalse((local / "frame").exists())
            self.assertTrue((local / "isolated" / "run.json").is_file())
            self.assertTrue(isolated.is_file())

    def test_removing_whole_object_selects_both_modes(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            isolated, _ = self._both_modes(project)
            local = isolated.parent.parent / "local"
            plan = plan_removal(project, scan_engravings(project), include_provenance=True)
            self.assertEqual(
                sorted(plan["provenance_roots"]),
                sorted([local / "isolated", local / "frame"]),
            )

    def test_doctor_accepts_valid_mode_aware_runs(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            self._both_modes(project)
            rebuild_index(project, "engravings", "movie")
            report = doctor(project, media_type="movie")
            checks = {f["check"] for f in report["findings"]}
            self.assertNotIn("legacy_provenance_layout", checks)
            self.assertNotIn("unpublished_run", checks)


class TestLegacyProvenance(unittest.TestCase):
    """Policy: legacy runs stay readable, and migrate deterministically."""

    def _legacy(self, project: Path, mode: str = "frame"):
        eng = _engraving(project, mode=mode)
        _run_record(project, eng.parent, mode=mode, legacy=True)
        return eng, eng.parent.parent / "local"

    def test_legacy_layout_is_discovered(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._legacy(project)
            self.assertEqual(len(find_legacy_runs(project, "movie")), 1)

    def test_legacy_run_is_claimed_only_by_its_own_mode(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _, local = self._legacy(project, mode="frame")
            frame_entry = scan_engravings(project, mode="frame")[0]
            self.assertEqual(provenance_dirs_for(frame_entry), [local])
            # An isolated engraving must not adopt the frame run.
            _engraving(project, mode="isolated")
            isolated_entry = scan_engravings(project, mode="isolated")[0]
            self.assertEqual(provenance_dirs_for(isolated_entry), [])

    def test_doctor_warns_about_legacy_layout_without_failing(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            self._legacy(project)
            rebuild_index(project, "engravings", "movie")
            report = doctor(project, media_type="movie")
            legacy = [f for f in report["findings"] if f["check"] == "legacy_provenance_layout"]
            self.assertEqual(len(legacy), 1)
            self.assertEqual(legacy[0]["severity"], WARNING)
            self.assertNotEqual(report["status"], BLOCKING)

    def test_migration_dry_run_moves_nothing(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _, local = self._legacy(project)
            result = migrate_legacy_run(local / "run.json")
            self.assertFalse(result["migrated"])
            self.assertEqual(result["mode"], "frame")
            self.assertTrue((local / "run.json").is_file())
            self.assertFalse((local / "frame").exists())

    def test_migration_preserves_bytes_and_updates_paths(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng, local = self._legacy(project)
            before = (local / "01-repair.png").read_bytes()
            stat_before = (local / "01-repair.png").stat().st_mtime

            result = migrate_legacy_run(local / "run.json", apply=True)
            self.assertTrue(result["migrated"])

            moved = local / "frame" / "01-repair.png"
            self.assertTrue(moved.is_file())
            self.assertEqual(moved.read_bytes(), before)
            self.assertAlmostEqual(moved.stat().st_mtime, stat_before, places=2)
            self.assertFalse((local / "run.json").exists())

            run = json.loads((local / "frame" / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["provenance_layout"], "mode-aware")
            self.assertIn("frame", run["run_json"])
            self.assertTrue(Path(run["outputs"]["final_png"]).is_file())

    def test_migration_repoints_published_provenance(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng, local = self._legacy(project)
            metadata = json.loads(eng.read_text(encoding="utf-8"))
            metadata["provenance"] = {"run_json": "x/local/run.json"}
            eng.write_text(json.dumps(metadata), encoding="utf-8")

            migrate_legacy_run(local / "run.json", apply=True)
            updated = json.loads(eng.read_text(encoding="utf-8"))
            self.assertEqual(updated["provenance"]["run_json"], "x/local/frame/run.json")

    def test_migration_never_overwrites_an_existing_mode_dir(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _, local = self._legacy(project)
            (local / "frame").mkdir()
            sentinel = local / "frame" / "run.json"
            sentinel.write_text('{"keep": true}', encoding="utf-8")

            result = migrate_legacy_run(local / "run.json", apply=True)
            self.assertFalse(result["migrated"])
            self.assertIn("already exists", result["reason"])
            self.assertEqual(json.loads(sentinel.read_text(encoding="utf-8")), {"keep": True})

    def test_migration_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _, local = self._legacy(project)
            migrate_legacy_run(local / "run.json", apply=True)
            self.assertEqual(find_legacy_runs(project, "movie"), [])

    def test_legacy_run_is_discoverable_as_a_cache_hit_before_migration(self):
        # An unmigrated run must keep working: resolve_run_paths adopts it for
        # its own mode so no regeneration is ever triggered by the layout change.
        from services.engraving_local_paths import resolve_run_paths

        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng, local = self._legacy(project, mode="frame")
            source_json = (
                project / "data" / "silhouettes" / "catalog" / "movie" / TITLE
                / "bird" / "object_0002.json"
            )
            record = {"media_type": "movie", "filename_stem": TITLE, "label": "bird"}

            paths, is_legacy = resolve_run_paths(project, source_json, record, "frame")
            self.assertTrue(is_legacy)
            self.assertEqual(paths["dir"], local)
            self.assertTrue(paths["run_json"].is_file())

    def test_migrated_run_is_still_discoverable_and_not_legacy(self):
        from services.engraving_local_paths import resolve_run_paths

        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            eng, local = self._legacy(project, mode="frame")
            migrate_legacy_run(local / "run.json", apply=True)

            source_json = (
                project / "data" / "silhouettes" / "catalog" / "movie" / TITLE
                / "bird" / "object_0002.json"
            )
            record = {"media_type": "movie", "filename_stem": TITLE, "label": "bird"}
            paths, is_legacy = resolve_run_paths(project, source_json, record, "frame")
            self.assertFalse(is_legacy)
            self.assertEqual(paths["dir"], local / "frame")
            self.assertTrue(paths["run_json"].is_file())

    def test_migration_keeps_every_recorded_hash_valid(self):
        import hashlib

        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _, local = self._legacy(project, mode="frame")
            repair = local / "01-repair.png"
            digest = hashlib.sha256(repair.read_bytes()).hexdigest()

            run = json.loads((local / "run.json").read_text(encoding="utf-8"))
            run["stages"] = [{
                "stage": "repair",
                "output": str(repair),
                "output_sha256": digest,
            }]
            (local / "run.json").write_text(json.dumps(run), encoding="utf-8")

            migrate_legacy_run(local / "run.json", apply=True)

            migrated = json.loads((local / "frame" / "run.json").read_text(encoding="utf-8"))
            stage = migrated["stages"][0]
            moved = Path(stage["output"])
            self.assertTrue(moved.is_file())
            self.assertEqual(hashlib.sha256(moved.read_bytes()).hexdigest(), stage["output_sha256"])

    def test_unknown_mode_is_refused(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _, local = self._legacy(project)
            (local / "run.json").write_text(json.dumps({"status": "needs_review"}), encoding="utf-8")
            result = migrate_legacy_run(local / "run.json", apply=True)
            self.assertFalse(result["migrated"])
            self.assertTrue((local / "run.json").is_file())


class TestPendingRecordChecks(unittest.TestCase):
    """Pending records must surface portability problems without false errors."""

    def _checks(self, report):
        return {finding["check"] for finding in report["findings"]}

    def _severity(self, report, check):
        return {f["severity"] for f in report["findings"] if f["check"] == check}

    def test_pending_with_valid_sources_is_clean(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            frame = project / "media" / "frames" / "f.png"
            _png(frame)
            eng = _engraving(project, status="pending", with_png=False)
            metadata = json.loads(eng.read_text(encoding="utf-8"))
            metadata["source"]["silhouette_png"] = (
                f"data/silhouettes/catalog/movie/{TITLE}/bird/object_0002.json"
            )
            metadata["source"]["source_frame"] = "media/frames/f.png"
            eng.write_text(json.dumps(metadata), encoding="utf-8")
            rebuild_index(project, "engravings", "movie")

            checks = self._checks(doctor(project, media_type="movie"))
            self.assertNotIn("stale_absolute_source_path", checks)
            self.assertNotIn("missing_source_path", checks)

    def test_pending_with_stale_absolute_source_frame_warns(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            eng = _engraving(project, status="pending", with_png=False)
            metadata = json.loads(eng.read_text(encoding="utf-8"))
            metadata["source"]["silhouette_png"] = (
                f"data/silhouettes/catalog/movie/{TITLE}/bird/object_0002.json"
            )
            # A path from a different machine's home layout.
            metadata["source"]["source_frame"] = "/elsewhere/projects/dead-crossing/media/f.png"
            eng.write_text(json.dumps(metadata), encoding="utf-8")
            rebuild_index(project, "engravings", "movie")

            report = doctor(project, media_type="movie")
            self.assertIn("stale_absolute_source_path", self._checks(report))
            self.assertEqual(self._severity(report, "stale_absolute_source_path"), {WARNING})
            self.assertNotEqual(report["status"], BLOCKING)

    def test_pending_without_output_is_not_an_error(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            _engraving(project, status="pending", with_png=False)
            rebuild_index(project, "engravings", "movie")
            report = doctor(project, media_type="movie")
            # A pending record legitimately has no canonical output yet.
            self.assertNotIn("missing_canonical_asset", self._checks(report))

    def test_generated_without_output_is_repairable(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            _engraving(project, status="generated", with_png=False)
            rebuild_index(project, "engravings", "movie")
            report = doctor(project, media_type="movie")
            self.assertIn("missing_canonical_asset", self._checks(report))
            self.assertEqual(self._severity(report, "missing_canonical_asset"), {REPAIRABLE})

    def test_malformed_source_path_is_blocking(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            eng = _engraving(project, status="pending", with_png=False)
            metadata = json.loads(eng.read_text(encoding="utf-8"))
            metadata["source"]["source_frame"] = {"unexpected": "object"}
            eng.write_text(json.dumps(metadata), encoding="utf-8")
            rebuild_index(project, "engravings", "movie")

            report = doctor(project, media_type="movie")
            self.assertIn("malformed_source_path", self._checks(report))
            self.assertEqual(self._severity(report, "malformed_source_path"), {BLOCKING})

    def test_doctor_reports_facts_not_guesses(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            _silhouette(project)
            eng = _engraving(project, status="pending", with_png=False)
            metadata = json.loads(eng.read_text(encoding="utf-8"))
            metadata["source"]["source_frame"] = "/elsewhere/media/f.png"
            eng.write_text(json.dumps(metadata), encoding="utf-8")
            rebuild_index(project, "engravings", "movie")

            finding = [
                f for f in doctor(project, media_type="movie")["findings"]
                if f["check"] == "stale_absolute_source_path"
            ][0]
            self.assertTrue(finding["absolute"])
            self.assertEqual(finding["status"], "pending")
            self.assertEqual(finding["key"], "source_frame")
            self.assertEqual(finding["path"], "/elsewhere/media/f.png")


class TestCacheIsModeSpecific(unittest.TestCase):
    """A cached isolated run must never be served as a frame run, or vice versa.

    No model is constructed: ``convert_one`` returns on the cache hit before it
    would instantiate the pipeline, so these run without any GPU work.
    """

    def _project_with_silhouette(self, tmp: str) -> tuple[Path, Path]:
        project = Path(tmp)
        directory = project / "data" / "silhouettes" / "catalog" / "movie" / TITLE / "bird"
        directory.mkdir(parents=True, exist_ok=True)

        silhouette = directory / "object_0002.png"
        image = Image.new("RGBA", (64, 96), (0, 0, 0, 0))
        for x in range(16, 48):
            for y in range(24, 72):
                image.putpixel((x, y), (60, 50, 40, 255))
        image.save(silhouette)

        source_json = directory / "object_0002.json"
        source_json.write_text(json.dumps({
            "media_type": "movie",
            "filename": f"{TITLE}.mp4",
            "filename_stem": TITLE,
            "media_id": "tmdb_61044",
            "shot_id": "tmdb_61044@f078383-f078476",
            "frame": 78453,
            "label": "bird",
            "field": "animals",
            "png": "object_0002.png",
        }), encoding="utf-8")
        return project, source_json

    def _seed_cached_run(self, project: Path, source_json: Path, mode: str) -> Path:
        from services.engraving_local_paths import engraving_paths

        record = json.loads(source_json.read_text(encoding="utf-8"))
        paths = engraving_paths(project, source_json, record, mode)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        _png(paths["final_png"])
        paths["run_json"].write_text(json.dumps({
            "status": "needs_review",
            "seed": 42,
            "mode": mode,
            "source_json": str(source_json),
            "run_json": str(paths["run_json"]),
            "provenance": {
                "label": "bird", "field": "animals", "media_type": "movie",
                "filename": f"{TITLE}.mp4", "filename_stem": TITLE,
                "media_id": "tmdb_61044", "shot_id": "tmdb_61044@f078383-f078476",
                "frame": 78453,
            },
            "inputs": {
                "silhouette_png": str(source_json.with_suffix(".png")),
                "source_frame": None, "context_frames": [],
            },
            "model": {"pipeline_repo": "stub", "pipeline_license": "apache-2.0",
                      "transformer_backend": "q8"},
            "stages": [{"stage": "repair"}],
            "validation": {"status": "needs_review"},
            "outputs": {"final_png": str(paths["final_png"])},
            "marker": mode,
        }), encoding="utf-8")
        return paths["run_json"]

    def _config(self, mode: str) -> dict:
        from services.engraving_local_config import resolve_config

        return resolve_config(None, {"evidence": {"use_source_frame": mode == "frame"}})

    def test_each_mode_hits_its_own_cache(self):
        from services.engraving_local_pipeline import convert_one

        with TemporaryDirectory() as tmp:
            project, source_json = self._project_with_silhouette(tmp)
            self._seed_cached_run(project, source_json, "isolated")
            self._seed_cached_run(project, source_json, "frame")

            isolated = convert_one(project, source_json, config=self._config("isolated"))
            self.assertTrue(isolated["reused_cache"])
            self.assertEqual(isolated["marker"], "isolated")

            frame = convert_one(project, source_json, config=self._config("frame"))
            self.assertTrue(frame["reused_cache"])
            self.assertEqual(frame["marker"], "frame")

    def test_cache_hit_publishes_the_matching_variant(self):
        from services.engraving_local_pipeline import convert_one

        with TemporaryDirectory() as tmp:
            project, source_json = self._project_with_silhouette(tmp)
            self._seed_cached_run(project, source_json, "isolated")
            self._seed_cached_run(project, source_json, "frame")

            convert_one(project, source_json, config=self._config("isolated"))
            convert_one(project, source_json, config=self._config("frame"))

            rebuild_index(project, "engravings", "movie")
            page = query_page(project, "engravings", "movie", limit=10)
            self.assertEqual(page["total"], 2)
            self.assertEqual(sorted(r["mode"] for r in page["records"]), ["frame", "isolated"])

    def test_rerunning_one_mode_leaves_the_other_untouched(self):
        from services.engraving_local_pipeline import convert_one

        with TemporaryDirectory() as tmp:
            project, source_json = self._project_with_silhouette(tmp)
            isolated_run = self._seed_cached_run(project, source_json, "isolated")
            frame_run = self._seed_cached_run(project, source_json, "frame")
            frame_before = frame_run.read_bytes()

            convert_one(project, source_json, config=self._config("isolated"))
            self.assertEqual(frame_run.read_bytes(), frame_before)

            isolated_before = isolated_run.read_bytes()
            convert_one(project, source_json, config=self._config("frame"))
            self.assertEqual(isolated_run.read_bytes(), isolated_before)

    def test_missing_mode_does_not_borrow_the_other_modes_cache(self):
        from services.engraving_local_paths import resolve_run_paths

        with TemporaryDirectory() as tmp:
            project, source_json = self._project_with_silhouette(tmp)
            self._seed_cached_run(project, source_json, "frame")

            record = json.loads(source_json.read_text(encoding="utf-8"))
            paths, legacy = resolve_run_paths(project, source_json, record, "isolated")
            self.assertFalse(legacy)
            self.assertFalse(paths["run_json"].is_file())
            self.assertEqual(paths["dir"].name, "isolated")


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
            self.assertIn("missing_source_path", self._checks(doctor(project, media_type="movie")))

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
