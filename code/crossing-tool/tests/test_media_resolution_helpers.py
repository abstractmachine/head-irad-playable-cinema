"""Tests for the shared single-film resolution helpers extracted from the
duplicated "resolve query -> exactly one metadata entry" blocks in
`cli.py` (`_resolve_single_normalize_match_or_exit`) and
`mcp_server/mcp_server.py` (`_resolve_single_film`).

Both helpers wrap the same de facto pattern that was previously copy-pasted
at each call site: no match -> report an error; more than one match ->
report an ambiguous-match error listing the candidates; exactly one match ->
return it. The CLI variant prints to stderr and calls ``sys.exit(1)``; the
MCP variant returns an error JSON string, matching each file's own existing
conventions (unchanged by this pass).
"""

import json

import pytest

import cli
from data.metadata import save_json_metadata
from mcp_server.mcp_server import _resolve_single_film


def _seed(tmp_path, records, media_type="movie"):
    save_json_metadata(str(tmp_path), media_type, records)


# ---------------------------------------------------------------------------
# cli.py — _resolve_single_normalize_match_or_exit
# ---------------------------------------------------------------------------

class TestResolveSingleNormalizeMatchOrExit:
    def test_unique_match_is_returned(self, tmp_path):
        _seed(tmp_path, [{"filename": "Film.mp4", "title": "A Film", "media_id": "tmdb_1"}])

        result = cli._resolve_single_normalize_match_or_exit(str(tmp_path), "movie", "A Film")

        assert len(result) == 1
        assert result[0]["filename"] == "Film.mp4"

    def test_no_match_exits_with_error(self, tmp_path, capsys):
        _seed(tmp_path, [{"filename": "Film.mp4", "title": "A Film", "media_id": "tmdb_1"}])

        with pytest.raises(SystemExit) as exc_info:
            cli._resolve_single_normalize_match_or_exit(str(tmp_path), "movie", "Nonexistent")

        assert exc_info.value.code == 1
        assert "No movie entries match" in capsys.readouterr().err

    def test_ambiguous_match_exits_and_lists_candidates(self, tmp_path, capsys):
        _seed(tmp_path, [
            {"filename": "Film One.mp4", "title": "Film One", "media_id": "tmdb_1"},
            {"filename": "Film Two.mp4", "title": "Film Two", "media_id": "tmdb_2"},
        ])

        with pytest.raises(SystemExit) as exc_info:
            cli._resolve_single_normalize_match_or_exit(str(tmp_path), "movie", "Film")

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "matches 2 movie entries" in err
        assert "Film One.mp4" in err
        assert "Film Two.mp4" in err


# ---------------------------------------------------------------------------
# mcp_server/mcp_server.py — _resolve_single_film
# ---------------------------------------------------------------------------

class TestResolveSingleFilm:
    def test_unique_match_returns_entry_and_filename(self, tmp_path):
        _seed(tmp_path, [{"filename": "Film.mp4", "title": "A Film", "media_id": "tmdb_1"}])

        result = _resolve_single_film(str(tmp_path), "A Film", "movie")

        assert not isinstance(result, str)
        entry, filename = result
        assert filename == "Film.mp4"
        assert entry["title"] == "A Film"

    def test_no_match_returns_error_json(self, tmp_path):
        _seed(tmp_path, [{"filename": "Film.mp4", "title": "A Film", "media_id": "tmdb_1"}])

        result = _resolve_single_film(str(tmp_path), "Nonexistent", "movie")

        assert isinstance(result, str)
        body = json.loads(result)
        assert body["ok"] is False
        assert "No film found matching" in body["error"]

    def test_ambiguous_match_returns_error_json_with_titles(self, tmp_path):
        _seed(tmp_path, [
            {"filename": "Film One.mp4", "title": "Film One", "media_id": "tmdb_1"},
            {"filename": "Film Two.mp4", "title": "Film Two", "media_id": "tmdb_2"},
        ])

        result = _resolve_single_film(str(tmp_path), "Film", "movie")

        assert isinstance(result, str)
        body = json.loads(result)
        assert body["ok"] is False
        assert "Ambiguous: 2 films match" in body["error"]
        assert "Film One" in body["detail"]
        assert "Film Two" in body["detail"]
