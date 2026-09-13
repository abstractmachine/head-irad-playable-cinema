"""CLI orchestration tests for canonical active silhouette discovery services."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import cli


@pytest.mark.parametrize(
    ("arguments", "service_name", "service_result"),
    [
        (
            ["index", "silhouette", "list", "gun", "--json"],
            "list_active_candidate_references",
            {
                "active_candidate_count": 1,
                "returned_count": 1,
                "entries": [{"object_id": "object_0001"}],
                "next_cursor": None,
            },
        ),
        (
            ["index", "silhouette", "summary", "gun", "--json"],
            "summarize_active_catalog",
            {"active_candidate_count": 1, "top_candidates": []},
        ),
        (
            ["index", "silhouette", "rank", "gun", "--json"],
            "rank_active_candidates",
            {"count": 1, "candidates": []},
        ),
        (
            ["index", "silhouette", "cluster", "gun", "--json"],
            "cluster_active_variants",
            {"cluster_count": 1, "clusters": []},
        ),
    ],
)
def test_cli_active_silhouette_commands_delegate_to_canonical_services(
    monkeypatch,
    capsys,
    arguments,
    service_name,
    service_result,
):
    args = cli.build_parser().parse_args(arguments)
    calls = []

    def canonical_service(project_path, **kwargs):
        calls.append((project_path, kwargs))
        return service_result

    monkeypatch.setattr(
        f"services.silhouette_discovery.{service_name}",
        canonical_service,
    )
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: "/project")

    cli._index_silhouette(args)

    assert calls and calls[0][0] == "/project"
    assert calls[0][1]["word"] == "gun"
    payload = json.loads(capsys.readouterr().out)
    assert payload == service_result


def test_cli_active_silhouette_list_maps_both_media_to_canonical_all_media(monkeypatch, capsys):
    args = cli.build_parser().parse_args([
        "index", "silhouette", "list", "gun", "--media", "both", "--json",
    ])
    calls = []

    def canonical_service(project_path, **kwargs):
        calls.append((project_path, kwargs))
        return {
            "active_candidate_count": 0,
            "returned_count": 0,
            "entries": [],
            "next_cursor": None,
        }

    monkeypatch.setattr(
        "services.silhouette_discovery.list_active_candidate_references",
        canonical_service,
    )
    monkeypatch.setattr(cli.prefs, "get", lambda key, default=None: "/project")

    cli._index_silhouette(args)

    assert calls[0][1]["media_type"] == "--all-media--"
    assert json.loads(capsys.readouterr().out)["active_candidate_count"] == 0