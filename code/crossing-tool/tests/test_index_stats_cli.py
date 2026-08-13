"""Tests for `crossing index stats`'s cached-stats CLI behavior.

Covers the "no artifact -> report missing/stale, never silently recompute"
rule at the CLI layer: `index stats` should read the persisted corpus-stats
cache when present and current, report an explicit missing/stale state
(exit 1) when it isn't, and only ever recompute when --force is passed.
"""

from __future__ import annotations

import json
import pytest
from argparse import Namespace
from unittest.mock import patch

import cli
from services.corpus_stats import CORPUS_STATS_SCHEMA_VERSION, corpus_stats_cache_path


def test_parser_exposes_index_stats_force_flag():
    args = cli.build_parser().parse_args(["index", "stats", "--force"])

    assert args.force is True


def test_parser_index_stats_force_defaults_to_false():
    args = cli.build_parser().parse_args(["index", "stats"])

    assert args.force is False


def test_cli_index_stats_reads_cache_without_recomputing(tmp_path, capsys):
    cached_stats = {"movie_videos": 314, "vocabulary_terms": 8418}

    def _fail(*args, **kwargs):
        raise AssertionError("get_corpus_stats must not run when a valid cache exists")

    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.corpus_stats.get_corpus_stats", side_effect=_fail),
        patch(
            "services.corpus_stats.get_corpus_stats_state",
            return_value={"state": "ready", "stats": cached_stats},
        ),
    ):
        cli._index_stats(Namespace(json=True, verbose=False, force=False))

    assert json.loads(capsys.readouterr().out) == cached_stats


def test_cli_index_stats_missing_cache_reports_missing_without_recomputing(tmp_path, capsys):
    """Without --force, a missing cache must be reported, never silently built."""

    def _fail(*args, **kwargs):
        raise AssertionError("get_corpus_stats must not run when no cache exists")

    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.corpus_stats.get_corpus_stats", side_effect=_fail),
        patch(
            "services.corpus_stats.get_corpus_stats_state",
            return_value={"state": "missing"},
        ),
    ):
        with pytest.raises(SystemExit):
            cli._index_stats(Namespace(json=False, verbose=False, force=False))

    err = capsys.readouterr().err
    assert "have not been built" in err.lower()
    assert "--force" in err


def test_cli_index_stats_stale_cache_reports_stale_without_recomputing(tmp_path, capsys):
    """Without --force, a stale cache must be reported, never silently rebuilt."""

    def _fail(*args, **kwargs):
        raise AssertionError("get_corpus_stats must not run when the cache is stale")

    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.corpus_stats.get_corpus_stats", side_effect=_fail),
        patch(
            "services.corpus_stats.get_corpus_stats_state",
            return_value={"state": "stale", "stats": {"movie_videos": 1}},
        ),
    ):
        with pytest.raises(SystemExit):
            cli._index_stats(Namespace(json=False, verbose=False, force=False))

    err = capsys.readouterr().err
    assert "stale" in err.lower()


def test_cli_index_stats_force_recomputes_even_with_valid_cache(tmp_path, capsys):
    cache_path = corpus_stats_cache_path(str(tmp_path))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"schema_version": CORPUS_STATS_SCHEMA_VERSION, "stats": {"movie_videos": 1}}),
        encoding="utf-8",
    )
    fresh_stats = {"movie_videos": 999}

    with (
        patch.object(cli.prefs, "get", return_value=str(tmp_path)),
        patch("services.corpus_stats.get_corpus_stats", return_value=fresh_stats) as get_stats,
    ):
        cli._index_stats(Namespace(json=True, verbose=False, force=True))

    get_stats.assert_called_once_with(str(tmp_path))
    assert json.loads(capsys.readouterr().out) == fresh_stats
    assert json.loads(cache_path.read_text())["stats"] == fresh_stats
