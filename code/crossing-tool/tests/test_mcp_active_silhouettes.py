"""MCP tests proving ordinary silhouette access is active Illustration-index only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

import mcp_server.mcp_server as mcp_server
from services.illustration_index import rebuild_index
from services.silhouette_discovery import list_active_candidate_references


class _FakeMCPImage:
    def __init__(self, *, data: bytes, format: str):
        self.data = data
        self.format = format


@pytest.fixture
def active_catalog(tmp_path, monkeypatch):
    directory = tmp_path / "data" / "silhouettes" / "catalog" / "movie" / "film" / "gun"
    directory.mkdir(parents=True)
    for index, state, confidence, area in (
        (1, "active", 0.5, 100),
        (2, "superseded", 0.99, 999),
        (3, "inactive", 0.98, 998),
    ):
        png_path = directory / f"object_{index:04d}.png"
        json_path = directory / f"object_{index:04d}.json"
        Image.new("RGBA", (12, 10), (index * 20, 20, 30, 180)).save(png_path)
        json_path.write_text(json.dumps({
            "schema_version": "1",
            "media_type": "movie",
            "filename": "film.mp4",
            "filename_stem": "film",
            "media_id": "tmdb_1",
            "shot_id": "tmdb_1@f000001-f000010",
            "frame": 5,
            "label": "gun",
            "field": "objects",
            "confidence": confidence,
            "bbox": [1, 2, 8, 7],
            "mask_area": area,
            "frame_size": [100, 100],
            "png": png_path.name,
            "assignment": {"state": state},
            "viewpoint": "side",
            "completeness": "full",
            "occlusion": "none",
            "isolation": "isolated",
            "usefulness_score": confidence,
            "isolation_score": 1.0,
            "completeness_score": 1.0,
            "occlusion_score": 1.0,
        }), encoding="utf-8")
    rebuild_index(tmp_path, "silhouettes", "movie")
    monkeypatch.setenv("CROSSING_PROJECT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_MCPImage", _FakeMCPImage)
    return tmp_path


def _payload(result: str) -> dict:
    return json.loads(result)


def test_mcp_normal_candidate_listing_excludes_historical_same_shot(active_catalog, monkeypatch):
    monkeypatch.setattr(
        "services.silhouette_catalog.iter_catalog",
        lambda *_args, **_kwargs: pytest.fail("ordinary MCP discovery must not traverse catalog"),
    )
    candidates = _payload(mcp_server.list_silhouette_candidates("gun", field="objects"))
    listing = _payload(mcp_server.list_silhouettes("gun", field="objects"))
    canonical = list_active_candidate_references(
        active_catalog, word="gun", field="objects", media_type="movie",
    )

    assert candidates["active_candidate_count"] == 1
    assert candidates["returned_count"] == 1
    assert [entry["object_id"] for entry in candidates["entries"]] == ["object_0001"]
    assert candidates["active_candidate_count"] == canonical["active_candidate_count"]
    assert candidates["entries"] == canonical["entries"]
    assert listing["count"] == 1
    assert [entry["object_id"] for entry in listing["entries"]] == ["object_0001"]


def test_mcp_summary_rank_and_clusters_exclude_historical_same_shot(active_catalog, monkeypatch):
    monkeypatch.setattr(
        "services.silhouette_catalog.iter_catalog",
        lambda *_args, **_kwargs: pytest.fail("ordinary MCP discovery must not traverse catalog"),
    )
    summary = _payload(mcp_server.summarize_silhouette_catalog("gun", field="objects"))
    ranking = _payload(mcp_server.rank_silhouette_candidates("gun", field="objects"))
    clusters = _payload(mcp_server.cluster_silhouette_variants("gun", field="objects"))

    assert summary["population"] == "active_illustration_index"
    assert summary["active_candidate_count"] == 1
    assert [entry["object_id"] for entry in summary["top_candidates"]] == ["object_0001"]
    assert [entry["object_id"] for entry in ranking["candidates"]] == ["object_0001"]
    assert clusters["cluster_count"] == 1
    assert clusters["clusters"][0]["representative"]["object_id"] == "object_0001"
    assert [entry["object_id"] for entry in clusters["clusters"][0]["candidates"]] == ["object_0001"]


def test_mcp_best_silhouette_does_not_select_larger_historical_asset(active_catalog, monkeypatch):
    monkeypatch.setattr(
        "services.silhouette_catalog.iter_catalog",
        lambda *_args, **_kwargs: pytest.fail("ordinary MCP discovery must not traverse catalog"),
    )
    monkeypatch.setattr(
        "services.frame_retrieval.retrieve_single_frame",
        lambda *_args, **_kwargs: {"image_data": b"frame"},
    )

    result = mcp_server.get_best_silhouette("gun", field="objects")

    metadata = json.loads(result[0])
    assert metadata["candidate"]["object_id"] == "object_0001"
    assert metadata["pixel_area"] == 100
    assert result[1].format == "png"


def test_mcp_archive_stats_reports_active_population_not_physical_rows(active_catalog, monkeypatch):
    monkeypatch.setattr(
        "services.silhouette_catalog.iter_catalog",
        lambda *_args, **_kwargs: pytest.fail("ordinary MCP discovery must not traverse catalog"),
    )
    stats = _payload(mcp_server.get_archive_stats("movie"))

    assert stats["silhouette_population"] == "active_illustration_index"
    assert stats["silhouette_index_status"] == "ready"
    assert stats["silhouette_entries"] == 1
    assert stats["silhouette_physical_index_records"] == 3


def test_mcp_candidate_pagination_remains_deterministic_after_active_filtering(
    active_catalog, monkeypatch,
):
    directory = active_catalog / "data" / "silhouettes" / "catalog" / "movie" / "film" / "gun"
    base = json.loads((directory / "object_0001.json").read_text(encoding="utf-8"))
    for index, confidence in ((4, 0.8), (5, 0.7)):
        png_path = directory / f"object_{index:04d}.png"
        json_path = directory / f"object_{index:04d}.json"
        png_path.write_bytes((directory / "object_0001.png").read_bytes())
        record = {
            **base,
            "frame": index,
            "confidence": confidence,
            "mask_area": index * 100,
            "png": png_path.name,
            "assignment": {"state": "active"},
        }
        json_path.write_text(json.dumps(record), encoding="utf-8")
    rebuild_index(active_catalog, "silhouettes", "movie")
    monkeypatch.setattr(
        "services.silhouette_catalog.iter_catalog",
        lambda *_args, **_kwargs: pytest.fail("ordinary MCP discovery must not traverse catalog"),
    )

    first = _payload(mcp_server.list_silhouette_candidates(
        "gun", field="objects", sort_by="score", descending=True, limit=2,
    ))
    second = _payload(mcp_server.list_silhouette_candidates(
        "gun", field="objects", sort_by="score", descending=True,
        limit=2, cursor=first["next_cursor"],
    ))

    assert first["active_candidate_count"] == 3
    assert [entry["object_id"] for entry in first["entries"]] == [
        "object_0004", "object_0005",
    ]
    assert first["next_cursor"] == "2"
    assert [entry["object_id"] for entry in second["entries"]] == ["object_0001"]
    assert second["next_cursor"] is None


def test_explicit_frame_retrieval_does_not_discover_silhouette_candidates(
    active_catalog,
    monkeypatch,
):
    monkeypatch.setattr(
        "services.frame_retrieval.retrieve_single_frame",
        lambda *_args, **_kwargs: {
            "film_title": "Film",
            "filename": "film.mp4",
            "shot_id": "tmdb_1@f000001-f000010",
            "start_time": "",
            "end_time": "",
            "image_data": b"frame",
            "mime_type": "image/jpeg",
            "metadata": {},
        },
    )

    result = mcp_server.get_best_frame(
        "film", "tmdb_1@f000001-f000010", media_type="movie",
    )

    assert len(result) == 2
    assert result[1].data == b"frame"


def test_active_silhouette_mcp_analysis_tools_are_registered():
    for name in (
        "list_silhouettes",
        "list_silhouette_candidates",
        "summarize_silhouette_catalog",
        "rank_silhouette_candidates",
        "cluster_silhouette_variants",
    ):
        tool = mcp_server.mcp._tool_manager.get_tool(name)
        assert tool is not None
        assert tool.output_schema is not None


def test_mcp_candidate_listing_reflects_the_canonical_service_result(monkeypatch, tmp_path):
    monkeypatch.setenv("CROSSING_PROJECT", str(tmp_path))
    expected = {
        "status": "ready",
        "media_type": "movie",
        "word": "gun",
        "field": "objects",
        "scope": "all",
        "sort_by": "catalog_order",
        "descending": False,
        "min_pixel_area": None,
        "min_score": None,
        "cursor": "0",
        "next_cursor": None,
        "active_candidate_count": 7,
        "returned_count": 1,
        "entries": [{"candidate_id": "canonical-service-result"}],
    }
    monkeypatch.setattr(
        "services.silhouette_discovery.list_active_candidate_references",
        lambda _project_path, **_kwargs: expected,
    )

    result = _payload(mcp_server.list_silhouette_candidates("gun", field="objects"))

    assert result["active_candidate_count"] == 7
    assert result["entries"] == [{"candidate_id": "canonical-service-result"}]
