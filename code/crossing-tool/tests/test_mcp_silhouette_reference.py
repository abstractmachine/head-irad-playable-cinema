"""MCP protocol tests for exact read-only silhouette reference packets."""

from __future__ import annotations

import asyncio
import json

from mcp.types import ImageContent, TextContent

import mcp_server.mcp_server as mcp_server
from services.silhouette_reference import SilhouetteReferenceError


class _FakeMCPImage:
    def __init__(self, *, data: bytes, format: str):
        self.data = data
        self.format = format


def _packet() -> dict:
    return {
        "metadata": {
            "ok": True,
            "candidate_ref": {"candidate_id": "data/silhouettes/catalog/movie/Film/gun/object_0001.json"},
            "source_frame": {"frame_index": 117, "is_exact_extraction_frame": True},
            "provenance": {"new_inference_performed": False, "selection_changed": False},
            "content_order": [
                "metadata", "silhouette", "exact_source_frame", "context_frame_01",
            ],
        },
        "content": [
            {"content_id": "silhouette", "data": b"silhouette-png", "format": "png"},
            {"content_id": "exact_source_frame", "data": b"exact-jpeg", "format": "jpeg"},
            {"content_id": "context_frame_01", "data": b"context-jpeg", "format": "jpeg"},
        ],
    }


def test_reference_packet_returns_metadata_then_ordered_images(monkeypatch, tmp_path):
    monkeypatch.setenv("CROSSING_PROJECT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_MCPImage", _FakeMCPImage)
    monkeypatch.setattr(
        "services.silhouette_reference.retrieve_silhouette_reference_packet",
        lambda project_path, **kwargs: _packet(),
    )

    result = mcp_server.get_silhouette_reference_packet(
        media_id="tmdb_1",
        shot_id="tmdb_1@f000100-f000140",
        frame_index=117,
        word="gun",
        field="objects",
    )

    metadata = json.loads(result[0])
    assert metadata["ok"] is True
    assert metadata["source_frame"]["is_exact_extraction_frame"] is True
    assert metadata["provenance"]["new_inference_performed"] is False
    assert metadata["content_order"] == [
        "metadata", "silhouette", "exact_source_frame", "context_frame_01",
    ]
    assert [(image.data, image.format) for image in result[1:]] == [
        (b"silhouette-png", "png"),
        (b"exact-jpeg", "jpeg"),
        (b"context-jpeg", "jpeg"),
    ]


def test_reference_packet_exposes_top_level_mcp_content_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv("CROSSING_PROJECT", str(tmp_path))
    monkeypatch.setattr(
        "services.silhouette_reference.retrieve_silhouette_reference_packet",
        lambda project_path, **kwargs: _packet(),
    )

    result = asyncio.run(
        mcp_server.mcp._tool_manager.call_tool(
            "get_silhouette_reference_packet",
            {
                "media_id": "tmdb_1",
                "shot_id": "tmdb_1@f000100-f000140",
                "frame_index": 117,
                "word": "gun",
                "field": "objects",
            },
            convert_result=True,
        )
    )

    assert isinstance(result[0], TextContent)
    assert [block.mimeType for block in result[1:]] == [
        "image/png", "image/jpeg", "image/jpeg",
    ]
    assert all(isinstance(block, ImageContent) for block in result[1:])


def test_reference_packet_returns_structured_resolution_error_without_images(monkeypatch, tmp_path):
    monkeypatch.setenv("CROSSING_PROJECT", str(tmp_path))
    error = SilhouetteReferenceError(
        "CANDIDATE_NOT_FOUND",
        "CANDIDATE_RESOLUTION",
        "No exact candidate exists.",
        {"frame_index": 117},
    )
    monkeypatch.setattr(
        "services.silhouette_reference.retrieve_silhouette_reference_packet",
        lambda project_path, **kwargs: (_ for _ in ()).throw(error),
    )

    result = mcp_server.get_silhouette_reference_packet(
        media_id="tmdb_1",
        shot_id="tmdb_1@f000100-f000140",
        frame_index=117,
        word="gun",
        field="objects",
    )

    assert len(result) == 1
    payload = json.loads(result[0])
    assert payload == error.payload()


def test_reference_packet_structures_project_context_and_encoding_errors(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_ctx",
        lambda: json.dumps({"ok": False, "error": "No project path configured."}),
    )

    context_result = mcp_server.get_silhouette_reference_packet(
        media_id="tmdb_1",
        shot_id="tmdb_1@f000100-f000140",
        frame_index=117,
        word="gun",
        field="objects",
    )

    context_error = json.loads(context_result[0])["error"]
    assert context_error["code"] == "PROJECT_CONTEXT_INVALID"
    assert context_error["stage"] == "FILM_RESOLUTION"

    monkeypatch.setattr(mcp_server, "_ctx", lambda: ("/project", ""))
    monkeypatch.setattr(
        "services.silhouette_reference.retrieve_silhouette_reference_packet",
        lambda project_path, **kwargs: (_ for _ in ()).throw(RuntimeError("image wrapper failed")),
    )

    encoding_result = mcp_server.get_silhouette_reference_packet(
        media_id="tmdb_1",
        shot_id="tmdb_1@f000100-f000140",
        frame_index=117,
        word="gun",
        field="objects",
    )

    encoding_error = json.loads(encoding_result[0])["error"]
    assert encoding_error["code"] == "MCP_IMAGE_ENCODING_FAILED"
    assert encoding_error["stage"] == "MCP_IMAGE_ENCODING"