"""Tests for the read-only MCP poster-retrieval tool."""

import asyncio
import json

from data.metadata import save_json_metadata
import mcp_server.mcp_server as mcp_server
from mcp.types import ImageContent, TextContent


class _FakeMCPImage:
    def __init__(self, *, data: bytes, format: str):
        self.data = data
        self.format = format


def _seed_project(tmp_path, monkeypatch, *, media_type="movie", records):
    monkeypatch.setenv("CROSSING_PROJECT", str(tmp_path))
    save_json_metadata(str(tmp_path), media_type, records)


def _write_poster(tmp_path, media_type, filename, content=b"poster-jpeg"):
    poster_path = (
        tmp_path
        / "media"
        / "thumbnails"
        / media_type
        / f"{filename.rsplit('.', 1)[0]}.jpg"
    )
    poster_path.parent.mkdir(parents=True, exist_ok=True)
    poster_path.write_bytes(content)
    return poster_path


def test_get_poster_returns_project_thumbnail_as_inline_jpeg(tmp_path, monkeypatch):
    filename = "The Searchers 1956.mp4"
    _seed_project(
        tmp_path,
        monkeypatch,
        records=[{"filename": filename, "title": "The Searchers", "year": 1956, "tmdb": 3110}],
    )
    _write_poster(tmp_path, "movie", filename)
    monkeypatch.setattr(mcp_server, "_MCPImage", _FakeMCPImage)

    result = mcp_server.get_poster("Searchers", year=1956)

    assert isinstance(result, list)
    metadata = json.loads(result[0])
    assert metadata["ok"] is True
    assert metadata["tmdb"] == 3110
    assert metadata["poster_path"] == "media/thumbnails/movie/The Searchers 1956.jpg"
    assert result[1].data == b"poster-jpeg"
    assert result[1].format == "jpeg"


def test_get_poster_emits_top_level_mcp_image_content(tmp_path, monkeypatch):
    filename = "Image Content.mp4"
    _seed_project(
        tmp_path,
        monkeypatch,
        records=[{"filename": filename, "title": "Image Content", "tmdb": 19}],
    )
    _write_poster(tmp_path, "movie", filename)

    result = asyncio.run(
        mcp_server.mcp._tool_manager.call_tool(
            "get_poster", {"film": "Image Content"}, convert_result=True
        )
    )

    assert len(result) == 2
    assert isinstance(result[0], TextContent)
    assert isinstance(result[1], ImageContent)
    assert result[1].mimeType == "image/jpeg"


def test_mixed_image_tools_disable_structured_output():
    image_tools = (
        "get_poster",
        "test_image_return",
        "test_image_png",
        "get_best_frame",
        "get_best_frames",
        "get_palette_frames",
        "get_motif_frames",
        "get_context_frames",
        "get_best_silhouette",
    )

    for name in image_tools:
        tool = mcp_server.mcp._tool_manager.get_tool(name)
        assert tool is not None
        assert tool.output_schema is None


def test_get_poster_accepts_tmdb_id_and_gameplay_thumbnail(tmp_path, monkeypatch):
    filename = "Celeste Gameplay.mp4"
    _seed_project(
        tmp_path,
        monkeypatch,
        media_type="gameplay",
        records=[{"filename": filename, "title": "Celeste", "year": 2018, "tmdb_id": 4567}],
    )
    _write_poster(tmp_path, "gameplay", filename, b"gameplay-poster")
    monkeypatch.setattr(mcp_server, "_MCPImage", _FakeMCPImage)

    result = mcp_server.get_poster("Celeste", media_type="gameplay", tmdb_id=4567)

    assert isinstance(result, list)
    metadata = json.loads(result[0])
    assert metadata["poster_path"] == "media/thumbnails/gameplay/Celeste Gameplay.jpg"
    assert result[1].data == b"gameplay-poster"


def test_list_movies_and_get_metadata_expose_poster_availability(tmp_path, monkeypatch):
    with_poster = "With Poster.mp4"
    without_poster = "Without Poster.mp4"
    _seed_project(
        tmp_path,
        monkeypatch,
        records=[
            {"filename": with_poster, "title": "With Poster", "tmdb": 1},
            {"filename": without_poster, "title": "Without Poster", "tmdb": 2},
        ],
    )
    _write_poster(tmp_path, "movie", with_poster)

    listing = json.loads(mcp_server.list_movies())
    listing_by_title = {entry["title"]: entry for entry in listing["movies"]}
    metadata = json.loads(mcp_server.get_metadata("With Poster"))

    assert listing_by_title["With Poster"]["has_poster"] is True
    assert listing_by_title["Without Poster"]["has_poster"] is False
    assert metadata["film"]["has_poster"] is True
    assert metadata["film"]["poster_path"] == "media/thumbnails/movie/With Poster.jpg"


def test_get_poster_reports_a_missing_local_thumbnail(tmp_path, monkeypatch):
    _seed_project(
        tmp_path,
        monkeypatch,
        records=[{"filename": "No Poster.mp4", "title": "No Poster", "tmdb": 3}],
    )

    result = mcp_server.get_poster("No Poster")

    assert isinstance(result, str)
    error = json.loads(result)
    assert error["ok"] is False
    assert "No local poster found" in error["error"]