"""Active-only Illustration-index discovery tests for ordinary MCP operations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from services.illustration_index import (
    ALL,
    invalidate_index,
    load_index,
    query_page,
    rebuild_index,
)
from services.silhouette_discovery import (
    ActiveSilhouetteIndexError,
    active_candidate_page,
    cluster_active_variants,
    rank_active_candidates,
    resolve_exact_active_candidate,
    summarize_active_catalog,
)


_MEDIA_ID = "tmdb_1"
_SHOT_ID = "tmdb_1@f000001-f000010"


def _write_candidate(
    project: Path,
    *,
    index: int,
    assignment_state: str,
    frame_index: int = 5,
    confidence: float = 0.5,
    mask_area: int = 100,
) -> Path:
    directory = project / "data" / "silhouettes" / "catalog" / "movie" / "film" / "gun"
    directory.mkdir(parents=True, exist_ok=True)
    png_path = directory / f"object_{index:04d}.png"
    json_path = directory / f"object_{index:04d}.json"
    Image.new("RGBA", (12, 10), (index * 20, 30, 40, 200)).save(png_path)
    json_path.write_text(json.dumps({
        "schema_version": "1",
        "media_type": "movie",
        "filename": "film.mp4",
        "filename_stem": "film",
        "media_id": _MEDIA_ID,
        "shot_id": _SHOT_ID,
        "frame": frame_index,
        "label": "gun",
        "field": "objects",
        "confidence": confidence,
        "bbox": [1, 2, 8, 7],
        "mask_area": mask_area,
        "frame_size": [100, 100],
        "png": png_path.name,
        "assignment": {"state": assignment_state},
        "viewpoint": "side",
        "completeness": "full",
        "occlusion": "none",
        "isolation": "isolated",
        "usefulness_score": confidence,
        "isolation_score": 1.0,
        "completeness_score": 1.0,
        "occlusion_score": 1.0,
    }), encoding="utf-8")
    return json_path


@pytest.fixture
def lifecycle_catalog(tmp_path):
    active = _write_candidate(
        tmp_path, index=1, assignment_state="active", confidence=0.6, mask_area=100,
    )
    superseded = _write_candidate(
        tmp_path, index=2, assignment_state="superseded", confidence=0.99, mask_area=999,
    )
    inactive = _write_candidate(
        tmp_path, index=3, assignment_state="inactive", confidence=0.98, mask_area=998,
    )
    rebuild_index(tmp_path, "silhouettes", "movie")
    return {"project": tmp_path, "active": active, "superseded": superseded, "inactive": inactive}


def _filters():
    return {"word": "gun", "field": "objects", "media_type": "movie"}


def test_active_page_uses_index_not_raw_catalog_and_excludes_history(lifecycle_catalog, monkeypatch):
    project = lifecycle_catalog["project"]
    monkeypatch.setattr(
        "services.silhouette_catalog.iter_catalog",
        lambda *_args, **_kwargs: pytest.fail("ordinary discovery must not traverse catalog"),
    )

    page = active_candidate_page(project, **_filters(), limit=20)
    physical = query_page(
        project, "silhouettes", "movie", label="gun", field="objects",
        assignment_state=ALL, limit=20,
    )

    assert page["total"] == 1
    assert [record["path"].name for record in page["records"]] == ["object_0001.json"]
    assert load_index(project, "silhouettes", "movie")["count"] == 3
    assert physical["total"] == 3


def test_active_summary_ranking_and_clustering_exclude_same_shot_history(lifecycle_catalog):
    project = lifecycle_catalog["project"]

    summary = summarize_active_catalog(project, **_filters(), top_n=5)
    ranked = rank_active_candidates(project, **_filters(), limit=5)
    clusters = cluster_active_variants(project, **_filters(), limit=5)

    assert summary["active_candidate_count"] == 1
    assert summary["distinct_active_films"] == 1
    assert summary["distinct_active_shots"] == 1
    assert summary["mask_area"] == {
        "min": 100.0, "max": 100.0, "mean": 100.0,
        "p25": 100.0, "p50": 100.0, "p75": 100.0,
    }
    assert [candidate["object_id"] for candidate in summary["top_candidates"]] == ["object_0001"]
    assert [candidate["object_id"] for candidate in ranked["candidates"]] == ["object_0001"]
    assert clusters["cluster_count"] == 1
    assert clusters["clusters"][0]["active_candidate_count"] == 1
    assert clusters["clusters"][0]["representative"]["object_id"] == "object_0001"
    assert [candidate["object_id"] for candidate in clusters["clusters"][0]["candidates"]] == ["object_0001"]


def test_exact_resolution_requires_an_active_index_record(lifecycle_catalog):
    project = lifecycle_catalog["project"]
    active_id = str(lifecycle_catalog["active"].relative_to(project))
    historical_id = str(lifecycle_catalog["superseded"].relative_to(project))

    active = resolve_exact_active_candidate(
        project,
        media_id=_MEDIA_ID,
        shot_id=_SHOT_ID,
        frame_index=5,
        word="gun",
        field="objects",
        media_type="movie",
        candidate_id=active_id,
    )
    assert active["path"].name == "object_0001.json"

    with pytest.raises(ActiveSilhouetteIndexError) as caught:
        resolve_exact_active_candidate(
            project,
            media_id=_MEDIA_ID,
            shot_id=_SHOT_ID,
            frame_index=5,
            word="gun",
            field="objects",
            media_type="movie",
            candidate_id=historical_id,
        )
    assert caught.value.code == "CANDIDATE_NOT_FOUND"


def test_active_pagination_is_deterministic_after_filtering(tmp_path):
    _write_candidate(tmp_path, index=1, assignment_state="active", frame_index=1, confidence=0.2, mask_area=20)
    _write_candidate(tmp_path, index=2, assignment_state="superseded", frame_index=2, confidence=0.99, mask_area=999)
    _write_candidate(tmp_path, index=3, assignment_state="active", frame_index=3, confidence=0.9, mask_area=90)
    _write_candidate(tmp_path, index=4, assignment_state="active", frame_index=4, confidence=0.7, mask_area=70)
    rebuild_index(tmp_path, "silhouettes", "movie")

    first = active_candidate_page(
        tmp_path, **_filters(), sort_by="score", descending=True, limit=2,
    )
    second = active_candidate_page(
        tmp_path, **_filters(), sort_by="score", descending=True,
        limit=2, cursor=first["next_cursor"],
    )
    filtered = active_candidate_page(
        tmp_path, **_filters(), sort_by="pixel_area", descending=False,
        min_pixel_area=50, min_score=0.6, limit=10,
    )

    assert [record["path"].name for record in first["records"]] == [
        "object_0003.json", "object_0004.json",
    ]
    assert first["next_cursor"] == "2"
    assert [record["path"].name for record in second["records"]] == [
        "object_0001.json",
    ]
    assert [record["path"].name for record in filtered["records"]] == [
        "object_0004.json", "object_0003.json",
    ]
    assert filtered["total"] == 2


def test_active_discovery_rejects_a_stale_index_without_catalog_fallback(
    lifecycle_catalog,
    monkeypatch,
):
    project = lifecycle_catalog["project"]
    invalidate_index(project, "silhouettes", "movie")
    monkeypatch.setattr(
        "services.silhouette_catalog.iter_catalog",
        lambda *_args, **_kwargs: pytest.fail("stale-index discovery must not scan catalog"),
    )

    with pytest.raises(ActiveSilhouetteIndexError) as caught:
        active_candidate_page(project, **_filters())

    assert caught.value.code == "ILLUSTRATION_INDEX_NOT_READY"
    assert caught.value.details["index_status"] == "stale"
