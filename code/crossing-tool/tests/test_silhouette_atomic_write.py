"""Tests for the atomic-write path in the silhouette catalog lifecycle.

The silhouette catalog's canonical per-object JSON (``object_NNNN.json``) is
rewritten at several pipeline stages (creation + inline semantic enrichment
in `services/silhouette_catalog.py`, batch re-enrichment in
`services/silhouette_semantics.py`, quality scoring in
`services/silhouette_scoring.py`, and human-curation marking in
`services/silhouette_curation.py`). The word/field/scope silhouette cache and
its `manifest.json` index in `services/silhouette.py` are a separate, related
catalog. All of these used to call `Path.write_text()` directly; they now all
go through the shared `data.annotate.atomic_write_text()` helper.

These tests exercise every write site that does not require loading a real
SAM3/CLIP model (those are forced into their existing no-model fallback
paths via monkeypatching `_load_clip_model`), confirming the on-disk format
is unchanged and no temp file is ever left behind.
"""

import json

import pytest
from PIL import Image

from services import silhouette as silhouette_mod
from services.silhouette_catalog import catalog_item_dir, scan_catalog
from services.silhouette_curation import set_human_best
from services.silhouette_scoring import compute_scores_for_catalog
from services.silhouette_semantics import enrich_catalog


def _make_catalog_entry(project_path, media_type, filename_stem, label, index, extra=None):
    """Write a minimal object PNG + JSON pair under the catalog and return the JSON path."""
    label_dir = catalog_item_dir(str(project_path), media_type, filename_stem, label)
    label_dir.mkdir(parents=True, exist_ok=True)
    stem = f"object_{index:04d}"

    png_path = label_dir / f"{stem}.png"
    img = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    for y in range(4, 16):
        for x in range(4, 16):
            img.putpixel((x, y), (255, 255, 255, 255))
    img.save(png_path)

    meta = {
        "schema_version": "1",
        "media_type": media_type,
        "filename": f"{filename_stem}.mp4",
        "filename_stem": filename_stem,
        "media_id": "tmdb_1",
        "shot_id": f"tmdb_1@f{index:06d}-f{index + 30:06d}",
        "frame": index * 30,
        "label": label,
        "field": "objects",
        "confidence": 0.8,
        "bbox": [4, 4, 12, 12],
        "mask_area": 144,
        "frame_size": [20, 20],
        "png": png_path.name,
    }
    if extra:
        meta.update(extra)

    json_path = label_dir / f"{stem}.json"
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return json_path


def _make_prompt_file(project_path) -> None:
    """Write a minimal silhouette prompt file so enrich_catalog() can load one."""
    prompts_dir = project_path / "prompts" / "silhouettes"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "silhouettes-2026-01-01-v1.txt").write_text(
        "[viewpoint]\n"
        "side = a {label} seen from the side\n"
        "[completeness]\n"
        "full = a complete {label}, fully visible\n"
        "[occlusion]\n"
        "none = a {label} with no occlusion\n"
        "[isolation]\n"
        "isolated = an isolated {label}\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _no_real_clip_model(monkeypatch):
    """Force every CLIP load in this test module to fail gracefully.

    Both `compute_scores_for_catalog()` and `enrich_catalog()` already handle
    a failed CLIP load by falling back to their geometry-only / zero-score
    paths, so this exercises the real write sites without needing a model.
    """
    def _raise(*args, **kwargs):
        raise RuntimeError("no CLIP model in tests")

    monkeypatch.setattr("services.frame_match._load_clip_model", _raise)


class TestSetHumanBest:
    def test_round_trip(self, tmp_path):
        json_path = _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)
        set_human_best(json_path, human_best=True)
        assert json.loads(json_path.read_text(encoding="utf-8"))["human_best"] is True

    def test_false_removes_field_rather_than_setting_false(self, tmp_path):
        json_path = _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)
        set_human_best(json_path, human_best=True)
        set_human_best(json_path, human_best=False)
        assert "human_best" not in json.loads(json_path.read_text(encoding="utf-8"))

    def test_no_temp_file_left_behind(self, tmp_path):
        json_path = _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)
        set_human_best(json_path, human_best=True)
        leftovers = [p for p in json_path.parent.iterdir() if p.suffix not in (".json", ".png")]
        assert leftovers == []

    def test_cleans_up_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        json_path = _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)
        original = json_path.read_text(encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr("data.annotate.os.replace", _boom)
        with pytest.raises(OSError):
            set_human_best(json_path, human_best=True)

        assert json_path.read_text(encoding="utf-8") == original
        leftovers = [p for p in json_path.parent.iterdir() if p.suffix not in (".json", ".png")]
        assert leftovers == []


class TestSaveSilhouetteManifest:
    def test_round_trip(self, tmp_path):
        manifest = {"entries": [{"word": "horse", "field": "objects", "scope": "global", "media_type": "movie"}]}
        silhouette_mod.save_silhouette_manifest(str(tmp_path), "movie", manifest)
        assert silhouette_mod.load_silhouette_manifest(str(tmp_path), "movie") == manifest

    def test_no_temp_file_left_behind(self, tmp_path):
        silhouette_mod.save_silhouette_manifest(str(tmp_path), "movie", {"entries": []})
        manifest_dir = tmp_path / "data" / "silhouettes" / "movie"
        manifest_path = manifest_dir / "manifest.json"
        leftovers = [p for p in manifest_dir.iterdir() if p != manifest_path]
        assert leftovers == []

    def test_cleans_up_and_preserves_original_on_failure(self, tmp_path, monkeypatch):
        silhouette_mod.save_silhouette_manifest(str(tmp_path), "movie", {"entries": []})
        manifest_dir = tmp_path / "data" / "silhouettes" / "movie"
        manifest_path = manifest_dir / "manifest.json"
        original = manifest_path.read_text(encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr("data.annotate.os.replace", _boom)
        with pytest.raises(OSError):
            silhouette_mod.save_silhouette_manifest(str(tmp_path), "movie", {"entries": [{"word": "x"}]})

        assert manifest_path.read_text(encoding="utf-8") == original
        leftovers = [p for p in manifest_dir.iterdir() if p != manifest_path]
        assert leftovers == []


class TestComputeScoresForCatalog:
    def test_persists_score_fields_without_a_clip_model(self, tmp_path):
        json_path = _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)

        result = compute_scores_for_catalog(str(tmp_path), media_type="movie")

        assert result["processed"] == 1
        meta = json.loads(json_path.read_text(encoding="utf-8"))
        assert meta["usefulness_score"] is not None
        assert meta["semantic_label_score"] == 0.0  # no CLIP model available

    def test_skips_already_scored_unless_rebuild(self, tmp_path):
        json_path = _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)
        compute_scores_for_catalog(str(tmp_path), media_type="movie")

        result = compute_scores_for_catalog(str(tmp_path), media_type="movie")
        assert result == {"processed": 0, "skipped": 1, "errors": 0}

        result = compute_scores_for_catalog(str(tmp_path), media_type="movie", rebuild=True)
        assert result["processed"] == 1

    def test_no_temp_file_left_behind(self, tmp_path):
        json_path = _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)
        compute_scores_for_catalog(str(tmp_path), media_type="movie")
        leftovers = [p for p in json_path.parent.iterdir() if p.suffix not in (".json", ".png")]
        assert leftovers == []


class TestEnrichCatalog:
    def test_persists_semantic_fields_without_a_clip_model(self, tmp_path):
        _make_prompt_file(tmp_path)
        json_path = _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)

        result = enrich_catalog(str(tmp_path), media_type="movie")

        assert result["processed"] == 1
        meta = json.loads(json_path.read_text(encoding="utf-8"))
        assert meta["semantic_version"] == "1"
        assert meta["viewpoint"] == "unknown"  # geometry-only fallback

    def test_skips_already_enriched_unless_rebuild(self, tmp_path):
        _make_prompt_file(tmp_path)
        json_path = _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)
        enrich_catalog(str(tmp_path), media_type="movie")

        result = enrich_catalog(str(tmp_path), media_type="movie")
        assert result == {"processed": 0, "skipped": 1, "errors": 0}

        result = enrich_catalog(str(tmp_path), media_type="movie", rebuild=True)
        assert result["processed"] == 1

    def test_no_temp_file_left_behind(self, tmp_path):
        _make_prompt_file(tmp_path)
        json_path = _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)
        enrich_catalog(str(tmp_path), media_type="movie")
        leftovers = [p for p in json_path.parent.iterdir() if p.suffix not in (".json", ".png")]
        assert leftovers == []


class TestScanCatalogStillWorksAfterAllWrites:
    def test_scan_reflects_every_stage(self, tmp_path):
        _make_prompt_file(tmp_path)
        _make_catalog_entry(tmp_path, "movie", "Film", "horse", 1)
        compute_scores_for_catalog(str(tmp_path), media_type="movie")
        enrich_catalog(str(tmp_path), media_type="movie")

        records = scan_catalog(str(tmp_path), media_type="movie")
        assert len(records) == 1
        rec = records[0]
        assert rec["usefulness_score"] is not None
        assert rec["semantic_version"] == "1"

        set_human_best(rec["path"], human_best=True)
        records = scan_catalog(str(tmp_path), media_type="movie")
        assert records[0]["human_best"] is True
