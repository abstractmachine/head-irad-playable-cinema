"""Tests for the Project Visualizer V0 column dashboard.

Covers three layers:
  - services.corpus_stats: ProjectColumn / tiered column data model, the
    persisted stats cache, and the illustration-index fast path that avoids
    re-scanning the whole silhouette catalog.
  - visualizers.project_visualizer's renderer (_ProjectColumnWidget) and its
    async loading pipeline (_ProjectColumnsWorker, loading state, per-tier
    signal updates) — opening the window must never block on project data.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

import pytest
from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QColor, QHelpEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLabel, QScrollArea, QTabBar, QToolTip

import services.corpus_stats as corpus_stats_mod
import services.illustration_index as illustration_index_mod
import services.silhouette_catalog as silhouette_catalog_mod
from styles import theme
from services.corpus_stats import (
    PROJECT_COLUMN_IDS_AND_TITLES,
    ProjectColumn,
    corpus_stats_cache_path,
    get_cached_project_columns,
    get_indexed_field_stats,
    get_illustration_stats,
    get_live_project_columns,
    get_project_columns,
    get_vocabulary_field_counts,
    load_cached_corpus_stats,
    refresh_corpus_stats_cache,
)

EXPECTED_COLUMN_IDS = (
    "movies", "gameplay", "shots", "vocabulary", "silhouettes",
    "engravings",
)


def _write_metadata(project_path, media_type, filenames):
    meta_dir = project_path / "data" / "metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    payload = {"version": "1", "media": [{"filename": f, "title": f} for f in filenames]}
    (meta_dir / f"{media_type}.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_annotations(project_path, media_type, filename, shot_count):
    ann_dir = project_path / "data" / "annotations" / "shots" / media_type
    ann_dir.mkdir(parents=True, exist_ok=True)
    payload = [{"shot": {"index": i}} for i in range(shot_count)]
    (ann_dir / f"{filename}.annotations.json").write_text(json.dumps(payload), encoding="utf-8")


def _media_items_datavis(count, media_type="movie"):
    items = [
        {
            "index": index,
            "title": f"{media_type.title()} {index}",
            "filename": f"{media_type}-{index}.mp4",
            "media_type": media_type,
            "media_id": f"{media_type}_{index}",
        }
        for index in range(count)
    ]
    return {"kind": "media_items", "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# Data model — live tier (Movies/Gameplay/Silhouettes/Engravings)
# ---------------------------------------------------------------------------

def test_get_live_project_columns_does_not_traverse_annotations(tmp_path, monkeypatch):
    _write_metadata(tmp_path, "movie", ["a.mp4", "b.mp4"])
    _write_metadata(tmp_path, "gameplay", ["c.mp4"])
    _write_annotations(tmp_path, "movie", "a", 3)
    _write_annotations(tmp_path, "movie", "b", 2)
    monkeypatch.setattr(
        corpus_stats_mod, "_aggregate_annotated_shots",
        lambda _path: pytest.fail("Project live tier traversed shot annotations"),
    )
    monkeypatch.setattr(
        corpus_stats_mod, "get_indexed_field_stats",
        lambda _path, source: {
            "state": "ready",
            "count": 7 if source == "silhouettes" else 3,
            "fields": (
                [{"field": "animals", "count": 7}]
                if source == "silhouettes"
                else [
                    {"field": "animals", "count": 2},
                    {"field": "objects", "count": 1},
                ]
            ),
        },
    )

    columns = get_live_project_columns(str(tmp_path))

    assert [c.id for c in columns] == [
        "movies", "gameplay", "silhouettes", "engravings",
    ]
    by_id = {c.id: c for c in columns}
    assert by_id["movies"].count == 2
    assert by_id["gameplay"].count == 1
    assert by_id["silhouettes"].count == 7
    assert by_id["engravings"].count == 3
    assert all(c.state == "ready" for c in columns)
    assert by_id["movies"].datavis == {
        "kind": "media_items",
        "count": 2,
        "items": [
            {
                "index": 0, "title": "a.mp4", "filename": "a.mp4",
                "media_type": "movie", "media_id": "",
                "thumbnail_foreground_rgb": None,
            },
            {
                "index": 1, "title": "b.mp4", "filename": "b.mp4",
                "media_type": "movie", "media_id": "",
                "thumbnail_foreground_rgb": None,
            },
        ],
    }
    assert by_id["gameplay"].datavis == {
        "kind": "media_items",
        "count": 1,
        "items": [{
            "index": 0, "title": "c.mp4", "filename": "c.mp4",
            "media_type": "gameplay", "media_id": "",
            "thumbnail_foreground_rgb": None,
        }],
    }
    assert by_id["silhouettes"].datavis == {
        "kind": "silhouette_fields",
        "fields": [{"field": "animals", "count": 7}],
    }
    assert by_id["engravings"].datavis == {
        "kind": "engraving_fields",
        "fields": [
            {"field": "animals", "count": 2},
            {"field": "objects", "count": 1},
        ],
    }


def test_media_item_payload_counts_reuse_the_loaded_metadata_collections(monkeypatch):
    calls = []

    def load_metadata(_project_path, media_type):
        calls.append(media_type)
        if media_type == "movie":
            return [
                {
                    "filename": f"movie-{index}.mp4",
                    "title": f"Movie {index}",
                    "year": 1939 + index,
                    "media_id": f"tmdb_{index}",
                }
                for index in range(314)
            ]
        return [
            {
                "filename": "gameplay-a.mp4", "title": "Gameplay A",
                "media_id": "game_a",
            },
            {
                "filename": "gameplay-b.mp4", "title": "Gameplay B",
                "media_id": "game_b",
            },
        ]

    monkeypatch.setattr(corpus_stats_mod, "load_json_metadata", load_metadata)
    monkeypatch.setattr(
        corpus_stats_mod, "get_illustration_stats",
        lambda _path: {"state": "ready", "count": 0, "labels": {}},
    )

    columns = get_live_project_columns("/fake/project")
    by_id = {column.id: column for column in columns}

    assert calls == ["movie", "gameplay"]
    assert by_id["movies"].count == 314
    assert by_id["movies"].datavis["count"] == len(by_id["movies"].datavis["items"]) == 314
    assert by_id["movies"].datavis["items"][0] == {
        "index": 0,
        "title": "Movie 0 (1939)",
        "filename": "movie-0.mp4",
        "media_type": "movie",
        "media_id": "tmdb_0",
        "thumbnail_foreground_rgb": None,
    }
    assert by_id["gameplay"].count == 2
    assert by_id["gameplay"].datavis["count"] == len(by_id["gameplay"].datavis["items"]) == 2
    assert by_id["gameplay"].datavis["items"][1] == {
        "index": 1,
        "title": "Gameplay B",
        "filename": "gameplay-b.mp4",
        "media_type": "gameplay",
        "media_id": "game_b",
        "thumbnail_foreground_rgb": None,
    }


def test_media_items_project_cached_thumbnail_foregrounds_for_both_media_types(
    monkeypatch,
):
    import data.palette as palette_mod

    metadata = {
        "movie": [{
            "filename": "movie.mp4", "title": "Movie", "media_id": "movie_id",
        }],
        "gameplay": [{
            "filename": "gameplay.mp4", "title": "Gameplay", "media_id": "game_id",
        }],
    }
    palettes = {
        ("movie_id", "movie"): {
            "source": "thumbnail",
            "thumbnail": {"foreground": {"rgb": [12, 34, 56]}},
        },
        ("game_id", "gameplay"): {
            "source": "thumbnail",
            "thumbnail": {"foreground": {"rgb": [210, 98, 7]}},
        },
    }
    lookups = []

    monkeypatch.setattr(
        corpus_stats_mod, "load_json_metadata",
        lambda _project_path, media_type: metadata[media_type],
    )
    monkeypatch.setattr(
        palette_mod, "load_thumbnail_palette",
        lambda project_path, media_id, media_type: (
            lookups.append((project_path, media_id, media_type))
            or palettes[(media_id, media_type)]
        ),
    )
    monkeypatch.setattr(
        corpus_stats_mod, "get_illustration_stats",
        lambda _path: {"state": "ready", "count": 0, "labels": {}},
    )

    columns = get_live_project_columns("/current/project")
    by_id = {column.id: column for column in columns}

    assert by_id["movies"].datavis["items"][0]["thumbnail_foreground_rgb"] == [
        12, 34, 56,
    ]
    assert by_id["gameplay"].datavis["items"][0]["thumbnail_foreground_rgb"] == [
        210, 98, 7,
    ]
    assert lookups == [
        ("/current/project", "movie_id", "movie"),
        ("/current/project", "game_id", "gameplay"),
    ]


def test_missing_thumbnail_palettes_remain_explicit_without_extraction(monkeypatch):
    import data.palette as palette_mod

    monkeypatch.setattr(
        corpus_stats_mod, "load_json_metadata",
        lambda _project_path, media_type: [{
            "filename": f"{media_type}.mp4",
            "title": media_type.title(),
            "media_id": f"{media_type}_id",
        }],
    )
    monkeypatch.setattr(
        palette_mod, "load_thumbnail_palette", lambda *_args: None,
    )
    monkeypatch.setattr(
        palette_mod, "create_thumbnail_palette",
        lambda *_args, **_kwargs: pytest.fail("GUI data refresh triggered extraction"),
    )
    monkeypatch.setattr(
        corpus_stats_mod, "get_illustration_stats",
        lambda _path: {"state": "ready", "count": 0, "labels": {}},
    )

    columns = get_live_project_columns("/current/project")
    media_items = [
        item
        for column in columns[:2]
        for item in column.datavis["items"]
    ]

    assert media_items
    assert all(item["thumbnail_foreground_rgb"] is None for item in media_items)


def test_get_live_project_columns_without_project_path_are_unavailable():
    columns = get_live_project_columns(None)

    assert all(c.count is None for c in columns)
    assert all(c.state == "unavailable" for c in columns)
    assert all(c.datavis == {"kind": "empty"} for c in columns)


def test_get_live_project_columns_empty_project_reports_zero_not_unavailable(tmp_path, monkeypatch):
    # An empty-but-real project directory has a known answer (zero) for
    # Movies/Gameplay. Shots is cache-owned and is not part of this tier.
    columns = get_live_project_columns(str(tmp_path))
    by_id = {c.id: c for c in columns}

    for col_id in ("movies", "gameplay"):
        assert by_id[col_id].count == 0
        assert by_id[col_id].state == "ready"

    assert by_id["movies"].datavis == {"kind": "media_items", "count": 0, "items": []}
    assert by_id["gameplay"].datavis == {"kind": "media_items", "count": 0, "items": []}
    for col_id, reason in (
        ("silhouettes", "silhouette_index_missing"),
        ("engravings", "engraving_index_missing"),
    ):
        assert by_id[col_id].count is None
        assert by_id[col_id].state == "unavailable"
        assert by_id[col_id].reason == reason
        assert by_id[col_id].datavis == {"kind": "empty"}


def test_indexed_field_stats_combine_media_with_deterministic_order(monkeypatch):
    def query(_project_path, source, media_type):
        assert source == "silhouettes"
        return {
            "status": "ready",
            "count": 3,
            "fields": (
                [{"field": "objects", "count": 2}, {"field": "animals", "count": 1}]
                if media_type == "movie"
                else [{"field": "animals", "count": 2}, {"field": "humans", "count": 1}]
            ),
        }

    monkeypatch.setattr(illustration_index_mod, "query_field_counts", query)

    assert get_indexed_field_stats("/fake/project", "silhouettes") == {
        "state": "ready",
        "count": 6,
        "fields": [
            {"field": "animals", "count": 3},
            {"field": "objects", "count": 2},
            {"field": "humans", "count": 1},
        ],
    }


def test_indexed_field_stats_preserve_valid_zero(monkeypatch):
    monkeypatch.setattr(
        illustration_index_mod, "query_field_counts",
        lambda *_args: {"status": "ready", "count": 0, "fields": []},
    )

    assert get_indexed_field_stats("/fake/project", "engravings") == {
        "state": "ready", "count": 0, "fields": [],
    }


def test_indexed_field_stats_preserve_values_from_usable_stale_index(monkeypatch):
    monkeypatch.setattr(
        illustration_index_mod, "query_field_counts",
        lambda _project_path, _source, media_type: (
            {
                "status": "stale", "count": 3,
                "fields": [{"field": "objects", "count": 3}],
            }
            if media_type == "movie"
            else {
                "status": "ready", "count": 1,
                "fields": [{"field": "animals", "count": 1}],
            }
        ),
    )

    assert get_indexed_field_stats("/fake/project", "silhouettes") == {
        "state": "stale",
        "reason": "silhouette_index_stale",
        "count": 4,
        "fields": [
            {"field": "objects", "count": 3},
            {"field": "animals", "count": 1},
        ],
    }


def test_indexed_field_stats_preserve_synthetic_silhouette_category(monkeypatch):
    monkeypatch.setattr(
        illustration_index_mod, "query_field_counts",
        lambda _project_path, _source, media_type: {
            "status": "ready",
            "count": 2 if media_type == "movie" else 1,
            "fields": (
                [{"field": "<untyped>", "count": 2, "synthetic": True}]
                if media_type == "movie"
                else [{"field": "objects", "count": 1}]
            ),
        },
    )

    assert get_indexed_field_stats("/fake/project", "silhouettes") == {
        "state": "ready",
        "count": 3,
        "fields": [
            {"field": "<untyped>", "count": 2, "synthetic": True},
            {"field": "objects", "count": 1},
        ],
    }


@pytest.mark.parametrize(
    ("source", "status", "expected"),
    [
        (
            "silhouettes", "missing",
            {"state": "unavailable", "reason": "silhouette_index_missing"},
        ),
        (
            "engravings", "stale",
            {"state": "stale", "reason": "engraving_index_stale"},
        ),
        (
            "engravings", "error",
            {"state": "unavailable", "reason": "engraving_index_error"},
        ),
    ],
)
def test_indexed_field_stats_surface_unusable_indexes(
    monkeypatch, source, status, expected,
):
    monkeypatch.setattr(
        illustration_index_mod, "query_field_counts",
        lambda *_args: {
            "status": status, "count": 0, "fields": [],
            **({"usable": False} if status == "stale" else {}),
        },
    )

    assert get_indexed_field_stats("/fake/project", source) == expected


def test_live_project_columns_keep_usable_stale_index_values(monkeypatch):
    monkeypatch.setattr(corpus_stats_mod, "load_json_metadata", lambda *_args: [])
    monkeypatch.setattr(
        corpus_stats_mod, "get_indexed_field_stats",
        lambda _path, source: {
            "state": "stale",
            "reason": f"{source[:-1]}_index_stale",
            "count": 4,
            "fields": [{"field": "objects", "count": 4}],
        },
    )

    columns = {column.id: column for column in get_live_project_columns("/project")}

    for source, kind in (
        ("silhouettes", "silhouette_fields"),
        ("engravings", "engraving_fields"),
    ):
        assert columns[source].state == "stale"
        assert columns[source].count == 4
        assert columns[source].datavis == {
            "kind": kind,
            "fields": [{"field": "objects", "count": 4}],
        }


def test_live_project_field_payloads_read_only_current_indexes(tmp_path, monkeypatch):
    indexed_records = {
        ("silhouettes", "movie"): [
            {"filename_stem": "film", "field": "animals", "label": "horse"},
            {"filename_stem": "film", "field": "objects", "label": "hat"},
        ],
        ("silhouettes", "gameplay"): [
            {"filename_stem": "game", "field": "animals", "label": "dog"},
        ],
        ("engravings", "movie"): [
            {"filename_stem": "film", "field": "humans", "label": "rider"},
            {"filename_stem": "film", "field": "humans", "label": "sheriff"},
        ],
        ("engravings", "gameplay"): [
            {"filename_stem": "game", "field": "animals", "label": "dog"},
        ],
    }
    for source in ("silhouettes", "engravings"):
        scan_name = f"_scan_{source}"
        for media_type in ("movie", "gameplay"):
            monkeypatch.setattr(
                illustration_index_mod, scan_name,
                lambda _project, _media_type, rows=indexed_records[(source, media_type)]: rows,
            )
            illustration_index_mod.rebuild_index(tmp_path, source, media_type)

    def fail_scan(*_args, **_kwargs):
        pytest.fail("Project refresh traversed a canonical source catalog")

    monkeypatch.setattr(illustration_index_mod, "_scan_silhouettes", fail_scan)
    monkeypatch.setattr(illustration_index_mod, "_scan_engravings", fail_scan)
    monkeypatch.setattr(silhouette_catalog_mod, "scan_catalog", fail_scan)

    columns = {column.id: column for column in get_live_project_columns(str(tmp_path))}

    assert columns["silhouettes"].count == 3
    assert columns["silhouettes"].datavis == {
        "kind": "silhouette_fields",
        "fields": [
            {"field": "animals", "count": 2},
            {"field": "objects", "count": 1},
        ],
    }
    assert columns["engravings"].count == 3
    assert columns["engravings"].datavis == {
        "kind": "engraving_fields",
        "fields": [
            {"field": "humans", "count": 2},
            {"field": "animals", "count": 1},
        ],
    }


# ---------------------------------------------------------------------------
# Data model — cached tier (Shots/Vocabulary)
# ---------------------------------------------------------------------------

def test_get_cached_project_columns_reads_stats_cache(monkeypatch):
    fake_stats = {
        "annotated_shots": 5,
        "annotated_shot_types": [
            {"name": "diegetic", "count": 3, "synthetic": False},
            {"name": "<untyped>", "count": 2, "synthetic": True},
        ],
        "vocabulary_terms": 1832,
    }
    monkeypatch.setattr(
        corpus_stats_mod, "get_corpus_stats_state",
        lambda p: {"state": "ready", "stats": fake_stats},
    )
    monkeypatch.setattr(
        corpus_stats_mod, "load_vocabulary_index",
        lambda p, media_type: {
            "meta": {"total_tokens": 1832, "vocabulary_fields": ["objects"]},
            "fields": {"objects": {str(index): 1 for index in range(1832)}},
        },
    )
    monkeypatch.setattr(corpus_stats_mod, "vocabulary_cache_is_stale", lambda p, media_type: False)

    columns = get_cached_project_columns("/fake/project")

    assert [c.id for c in columns] == ["shots", "vocabulary"]
    counts = {c.id: c.count for c in columns}
    assert counts == {"shots": 5, "vocabulary": 1832}
    assert all(c.state == "ready" for c in columns)
    assert columns[0].datavis == {
        "kind": "shot_types",
        "fields": fake_stats["annotated_shot_types"],
    }
    assert sum(field["count"] for field in columns[0].datavis["fields"]) == columns[0].count
    assert columns[1].datavis == {
        "kind": "vocabulary_fields",
        "fields": [{"field": "objects", "count": 1832}],
    }


def test_get_cached_project_columns_keeps_stale_vocabulary_distinct(monkeypatch):
    monkeypatch.setattr(
        corpus_stats_mod, "get_corpus_stats_state",
        lambda p: {
            "state": "ready",
            "stats": {
                "annotated_shots": 0, "annotated_shot_types": [],
                "vocabulary_terms": 2,
            },
        },
    )
    monkeypatch.setattr(
        corpus_stats_mod, "load_vocabulary_index",
        lambda p, media_type: {
            "meta": {"total_tokens": 2, "vocabulary_fields": ["objects"]},
            "fields": {"objects": {"chair": {}, "table": {}}},
        },
    )
    monkeypatch.setattr(corpus_stats_mod, "vocabulary_cache_is_stale", lambda p, media_type: True)

    columns = get_cached_project_columns("/fake/project")

    assert columns[1].count is None
    assert columns[1].state == "stale"
    assert columns[1].reason == "vocabulary_index_stale"
    assert columns[1].datavis == {"kind": "empty"}
    assert (columns[0].id, columns[0].count, columns[0].state) == (
        "shots", 0, "ready",
    )


def test_get_cached_project_columns_surfaces_vocabulary_count_mismatch(monkeypatch):
    monkeypatch.setattr(
        corpus_stats_mod, "get_corpus_stats_state",
        lambda p: {
            "state": "ready",
            "stats": {
                "annotated_shots": 0, "annotated_shot_types": [],
                "vocabulary_terms": 3,
            },
        },
    )
    monkeypatch.setattr(
        corpus_stats_mod, "load_vocabulary_index",
        lambda p, media_type: {
            "meta": {"total_tokens": 2, "vocabulary_fields": ["objects"]},
            "fields": {"objects": {"chair": {}, "table": {}}},
        },
    )

    columns = get_cached_project_columns("/fake/project")

    assert columns[1].count is None
    assert columns[1].state == "stale"
    assert columns[1].reason == "vocabulary_count_mismatch"
    assert columns[1].datavis == {"kind": "empty"}
    assert (columns[0].id, columns[0].count) == ("shots", 0)


def test_get_cached_project_columns_rejects_mismatched_shot_type_total(monkeypatch):
    monkeypatch.setattr(
        corpus_stats_mod, "get_corpus_stats_state",
        lambda _path: {
            "state": "ready",
            "stats": {
                "annotated_shots": 3,
                "annotated_shot_types": [
                    {"name": "diegetic", "count": 2, "synthetic": False},
                ],
                "vocabulary_terms": 0,
            },
        },
    )
    monkeypatch.setattr(
        corpus_stats_mod, "load_vocabulary_index",
        lambda *_args: {"meta": {"total_tokens": 0}, "fields": {}},
    )
    monkeypatch.setattr(corpus_stats_mod, "vocabulary_cache_is_stale", lambda *_args: False)

    shots = get_cached_project_columns("/fake/project")[0]

    assert shots.count is None
    assert shots.state == "stale"
    assert shots.reason == "shot_type_stats_invalid"
    assert shots.datavis == {"kind": "empty"}


def test_vocabulary_field_counts_sort_by_count_and_match_primary_total(monkeypatch):
    monkeypatch.setattr(
        corpus_stats_mod, "load_vocabulary_index",
        lambda p, media_type: {
            "meta": {
                "total_tokens": 6,
                "vocabulary_fields": ["settings", "objects", "animals"],
            },
            "fields": {
                "objects": {"chair": {}, "table": {}, "lamp": {}},
                "animals": {"horse": {}},
                "settings": {"interior": {}, "exterior": {}},
            },
        },
    )

    assert get_vocabulary_field_counts("/fake/project", expected_total=6) == [
        {"field": "objects", "count": 3},
        {"field": "settings", "count": 2},
        {"field": "animals", "count": 1},
    ]


def test_vocabulary_field_counts_surface_total_discrepancy(monkeypatch):
    monkeypatch.setattr(
        corpus_stats_mod, "load_vocabulary_index",
        lambda p, media_type: {
            "meta": {"total_tokens": 3, "vocabulary_fields": ["objects"]},
            "fields": {"objects": {"chair": {}, "table": {}}},
        },
    )

    with pytest.raises(ValueError, match="field total 2 does not match index total 3"):
        get_vocabulary_field_counts("/fake/project", expected_total=3)


def test_get_cached_project_columns_unavailable_without_cache(monkeypatch):
    monkeypatch.setattr(corpus_stats_mod, "get_corpus_stats_state", lambda p: {"state": "missing"})

    columns = get_cached_project_columns("/fake/project")

    assert all(c.count is None for c in columns)
    assert all(c.state == "unavailable" for c in columns)
    assert all(c.reason == "corpus_stats_missing" for c in columns)


def test_get_cached_project_columns_stale_without_recomputing(monkeypatch):
    monkeypatch.setattr(
        corpus_stats_mod, "get_corpus_stats_state",
        lambda p: {"state": "stale", "stats": {"vocabulary_terms": 1}},
    )

    columns = get_cached_project_columns("/fake/project")

    assert all(c.count is None for c in columns)
    assert all(c.state == "stale" for c in columns)
    assert all(c.reason == "corpus_stats_stale" for c in columns)


# ---------------------------------------------------------------------------
# Data model — combined + the fixed header order used for GUI placeholders
# ---------------------------------------------------------------------------

def test_get_project_columns_combines_both_tiers_in_fixed_order(monkeypatch):
    monkeypatch.setattr(
        corpus_stats_mod, "get_corpus_stats_state",
        lambda p: {"state": "ready", "stats": {"vocabulary_terms": 1}},
    )

    columns = get_project_columns(None)  # live tier -> unavailable, cached tier -> ready

    assert [c.id for c in columns] == list(EXPECTED_COLUMN_IDS)
    assert [c.title for c in columns] == [
        "Movies", "Gameplay", "Shots", "Vocabulary", "Silhouettes", "Engravings",
    ]
    assert all(c.datavis == {"kind": "empty"} for c in columns)


def test_project_column_ids_and_titles_matches_get_project_columns_order():
    columns = get_project_columns(None)

    assert PROJECT_COLUMN_IDS_AND_TITLES == tuple((c.id, c.title) for c in columns)
    assert "segments" not in EXPECTED_COLUMN_IDS
    assert "flipbooks" not in EXPECTED_COLUMN_IDS
    assert "illustrations" not in EXPECTED_COLUMN_IDS


# ---------------------------------------------------------------------------
# Persisted stats cache
# ---------------------------------------------------------------------------

def test_refresh_corpus_stats_cache_writes_and_load_reads_it_back(tmp_path, monkeypatch):
    fake_stats = {
        "movie_videos": 9,
        "annotated_shots": 5,
        "annotated_shot_types": [
            {"name": "diegetic", "count": 4, "synthetic": False},
            {"name": "<untyped>", "count": 1, "synthetic": True},
        ],
        "vocabulary_terms": 5,
    }
    monkeypatch.setattr(corpus_stats_mod, "get_corpus_stats", lambda p: fake_stats)

    result = refresh_corpus_stats_cache(str(tmp_path))

    assert result == fake_stats
    assert corpus_stats_cache_path(str(tmp_path)).exists()
    assert load_cached_corpus_stats(str(tmp_path)) == fake_stats


def test_load_cached_corpus_stats_missing_file_returns_none(tmp_path):
    assert load_cached_corpus_stats(str(tmp_path)) is None


def test_load_cached_corpus_stats_wrong_schema_version_returns_none(tmp_path):
    path = corpus_stats_cache_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": -1, "stats": {"a": 1}}), encoding="utf-8")

    assert load_cached_corpus_stats(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# Illustration index — never falls back to scanning the silhouette catalog
# ---------------------------------------------------------------------------

def test_get_illustration_stats_ready_combines_both_media_types(monkeypatch):
    monkeypatch.setattr(
        illustration_index_mod, "load_index",
        lambda project_path, source, media_type: {"status": "ready", "count": 10, "usable": True},
    )
    monkeypatch.setattr(
        illustration_index_mod, "query_facets",
        lambda project_path, source, media_type: {"labels": [{"label": "horse", "count": 7}]},
    )

    result = get_illustration_stats("/fake/project")

    # Summed across both media types (movie + gameplay), since the fake
    # index reports the same values for either.
    assert result["state"] == "ready"
    assert result["count"] == 20
    assert result["labels"] == {"horse": 14}


def test_get_illustration_stats_missing_index_reports_unavailable_without_scanning(monkeypatch):
    monkeypatch.setattr(
        illustration_index_mod, "load_index",
        lambda project_path, source, media_type: {"status": "missing", "count": 0, "usable": False},
    )

    def _fail(*args, **kwargs):
        raise AssertionError("audit_catalog must never be called")

    monkeypatch.setattr(silhouette_catalog_mod, "audit_catalog", _fail)

    result = get_illustration_stats("/fake/project")

    assert result == {"state": "unavailable", "reason": "illustration_index_missing"}


def test_get_illustration_stats_stale_index_reports_stale_without_scanning(monkeypatch):
    def _load_index(project_path, source, media_type):
        if media_type == "movie":
            return {"status": "stale", "count": 5, "usable": True}
        return {"status": "ready", "count": 3, "usable": True}

    monkeypatch.setattr(illustration_index_mod, "load_index", _load_index)
    monkeypatch.setattr(
        illustration_index_mod, "query_facets",
        lambda project_path, source, media_type: {"labels": []},
    )

    def _fail(*args, **kwargs):
        raise AssertionError("audit_catalog must never be called")

    monkeypatch.setattr(silhouette_catalog_mod, "audit_catalog", _fail)

    result = get_illustration_stats("/fake/project")

    assert result == {
        "state": "stale",
        "reason": "illustration_index_stale",
        "count": 8,
        "labels": {},
    }


def test_stale_project_column_keeps_status_and_previous_datavis(app):
    from visualizers.project_visualizer import _ProjectColumnWidget

    column = ProjectColumn(
        id="silhouettes", title="Silhouettes", count=4,
        datavis={
            "kind": "silhouette_fields",
            "fields": [
                {"field": "objects", "count": 3},
                {"field": "animals", "count": 1},
            ],
        },
        state="stale", reason="silhouette_index_stale",
    )
    widget = _ProjectColumnWidget(column)
    try:
        widget.resize(140, 258)
        widget.show()
        app.processEvents()

        assert widget._count_label.text() == "INDEX STALE"
        assert f"background: {theme.WARNING_COLOR}" in widget._count_label.styleSheet()
        assert [cell.field_label.text() for cell in widget._datavis_widget._field_cells] == [
            "objects", "animals",
        ]
        assert [cell.count_label.text() for cell in widget._datavis_widget._field_cells] == [
            "3", "1",
        ]
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_get_illustration_stats_no_project_path_is_unavailable():
    assert get_illustration_stats(None) == {"state": "unavailable", "reason": "no_project"}


# ---------------------------------------------------------------------------
# Renderer: visualizers/project_visualizer.py
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


@pytest.fixture
def fake_prefs(monkeypatch):
    store = {}
    monkeypatch.setattr("tool.prefs.get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr("tool.prefs.set", lambda key, value: store.__setitem__(key, value))
    return store


def test_project_visualizer_headers_appear_immediately_in_loading_state(app, fake_prefs, monkeypatch):
    fake_prefs["path"] = "/fake/project"
    # Prevent the real background thread from running so we can inspect the
    # window in its just-constructed, pre-worker-completion state.
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        widgets = window._project_column_widgets
        assert list(widgets.keys()) == list(EXPECTED_COLUMN_IDS)

        layout = window._browser.layout()
        assert layout.count() == 6
        assert all(layout.stretch(i) == 1 for i in range(6))  # equal-width, responsive grid

        for col_id, title in PROJECT_COLUMN_IDS_AND_TITLES:
            widget = widgets[col_id]
            assert widget.column.state == "loading"
            assert widget._header_label.text() == title.upper()
            assert widget._count_label.text() == "loading…"
            assert widget._loading_bar._active is True
    finally:
        window.close()


def test_project_tools_section_contains_two_by_two_tool_buttons(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        assert window._tools_section._title == "Tools"
        assert window._tools_section._pref_key == "project_section_tools"
        assert window.thumbnail_palettes_btn.text() == "Index Thumbnails"
        assert window.thumbnail_palettes_btn.isEnabled()
        assert window.rebuild_vocabulary_btn.text() == "Index Vocabulary"
        assert window.rebuild_vocabulary_btn.isEnabled()
        assert window.index_illustrations_btn.text() == "Index Illustrations"
        assert window.index_illustrations_btn.isEnabled()
        assert window.index_all_btn.text() == "Index"
        assert window.index_all_btn.isEnabled()
        buttons_layout = window._tools_buttons_widget.layout()
        assert buttons_layout.count() == 4
        assert buttons_layout.itemAtPosition(0, 0).widget() is window.thumbnail_palettes_btn
        assert buttons_layout.itemAtPosition(0, 1).widget() is window.rebuild_vocabulary_btn
        assert buttons_layout.itemAtPosition(1, 0).widget() is window.index_illustrations_btn
        assert buttons_layout.itemAtPosition(1, 1).widget() is window.index_all_btn
        assert buttons_layout.columnStretch(0) == buttons_layout.columnStretch(1) == 1
        assert not window._thumbnail_palette_poll_timer.isActive()
        assert not window._vocabulary_poll_timer.isActive()
        assert not window._illustration_poll_timer.isActive()
        assert not window._index_all_poll_timer.isActive()
    finally:
        window.close()


def test_project_inspector_tab_shows_f1_suffix(app, fake_prefs, monkeypatch):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        tab_bar = window._inspector_shell.tabbed_panel().tab_bar()
        assert tab_bar.count() == 1
        assert tab_bar.tabText(0) == "Project"
        shortcut = tab_bar.tabButton(0, QTabBar.RightSide)
        assert isinstance(shortcut, QLabel)
        assert shortcut.text() == "F1"
        assert f"color: {theme.TEXT_DIM};" in shortcut.styleSheet()
    finally:
        window.close()


@pytest.mark.parametrize("media_type", ["movie", "gameplay"])
def test_thumbnail_palette_cli_uses_canonical_argv_and_current_project(
    monkeypatch, media_type,
):
    from visualizers.project_visualizer import (
        _CLI_PATH,
        _start_thumbnail_palette_cli,
    )

    sentinel = object()
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return sentinel

    monkeypatch.setattr(
        "visualizers.project_visualizer.subprocess.Popen", fake_popen,
    )
    output_stream = object()

    result = _start_thumbnail_palette_cli(
        "/current/project", media_type, output_stream,
    )

    assert result is sentinel
    assert calls == [(
        [
            sys.executable,
            str(_CLI_PATH),
            "index", "palette", "create",
            "--thumbnail", "--all",
            "--media", media_type,
        ],
        {
            "cwd": "/current/project",
            "stdout": output_stream,
            "stderr": subprocess.STDOUT,
        },
    )]


def test_vocabulary_cli_uses_canonical_argv_and_current_project(monkeypatch):
    from visualizers.project_visualizer import _CLI_PATH, _start_vocabulary_cli

    sentinel = object()
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return sentinel

    monkeypatch.setattr(
        "visualizers.project_visualizer.subprocess.Popen", fake_popen,
    )
    output_stream = object()

    result = _start_vocabulary_cli("/current/project", output_stream)

    assert result is sentinel
    assert calls == [(
        [
            sys.executable,
            str(_CLI_PATH),
            "index", "vocabulary", "--all", "--force",
        ],
        {
            "cwd": "/current/project",
            "stdout": output_stream,
            "stderr": subprocess.STDOUT,
        },
    )]


def test_illustration_cli_uses_both_media_and_current_project(monkeypatch):
    from visualizers.project_visualizer import _CLI_PATH, _start_illustration_cli

    sentinel = object()
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return sentinel

    monkeypatch.setattr(
        "visualizers.project_visualizer.subprocess.Popen", fake_popen,
    )
    output_stream = object()

    result = _start_illustration_cli("/current/project", output_stream)

    assert result is sentinel
    assert calls == [(
        [
            sys.executable,
            str(_CLI_PATH),
            "index", "illustration", "--media", "both",
        ],
        {
            "cwd": "/current/project",
            "stdout": output_stream,
            "stderr": subprocess.STDOUT,
        },
    )]


@pytest.mark.parametrize("source", ["shot", "silhouettes"])
def test_untyped_audit_cli_uses_canonical_argv_and_current_project(
    monkeypatch, source,
):
    from visualizers.project_visualizer import _CLI_PATH, _start_untyped_audit_cli

    sentinel = object()
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return sentinel

    monkeypatch.setattr(
        "visualizers.project_visualizer.subprocess.Popen", fake_popen,
    )

    result = _start_untyped_audit_cli("/current/project", source)

    assert result is sentinel
    assert calls == [(
        [
            sys.executable,
            str(_CLI_PATH),
            "index", "untyped", "--source", source,
        ],
        {"cwd": "/current/project"},
    )]


def test_vocabulary_tool_runs_once_then_refreshes_project_columns(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/current/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import ProjectVisualizer

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

    process = FakeProcess()
    launches = []

    def fake_start(project_path, output_stream):
        launches.append((project_path, output_stream))
        return process

    monkeypatch.setattr(
        "visualizers.project_visualizer._start_vocabulary_cli", fake_start,
    )
    window = ProjectVisualizer()
    refreshes = []
    monkeypatch.setattr(
        window, "_start_project_columns_load",
        lambda *, force=False: refreshes.append(force),
    )
    try:
        QTest.mouseClick(window.rebuild_vocabulary_btn, Qt.LeftButton)

        assert len(launches) == 1
        assert launches[0][0] == "/current/project"
        assert not window.rebuild_vocabulary_btn.isEnabled()
        assert window.rebuild_vocabulary_btn.text() == "indexing"
        assert not window.thumbnail_palettes_btn.isEnabled()
        assert not window.index_illustrations_btn.isEnabled()
        assert not window.project_browse_btn.isEnabled()
        assert window._tools_loading_bar._active
        assert window._vocabulary_poll_timer.isActive()

        window._on_rebuild_vocabulary()
        window._on_thumbnail_palettes()
        assert len(launches) == 1

        process.returncode = 0
        window._poll_vocabulary_cli()

        assert window.rebuild_vocabulary_btn.isEnabled()
        assert window.rebuild_vocabulary_btn.text() == "Index Vocabulary"
        assert window.thumbnail_palettes_btn.isEnabled()
        assert window.index_illustrations_btn.isEnabled()
        assert window.project_browse_btn.isEnabled()
        assert not window._tools_loading_bar._active
        assert not window._vocabulary_poll_timer.isActive()
        assert refreshes == [True]
    finally:
        window.close()


def test_vocabulary_tool_restores_buttons_and_surfaces_cli_failure(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/current/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import ProjectVisualizer

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

    process = FakeProcess()

    def fake_start(_project_path, output_stream):
        output_stream.write(b"Vocabulary rebuild failed in CLI")
        return process

    messages = []
    monkeypatch.setattr(
        "visualizers.project_visualizer._start_vocabulary_cli", fake_start,
    )
    monkeypatch.setattr(
        "visualizers.project_visualizer.QMessageBox.critical",
        lambda parent, title, message: messages.append((title, message)),
    )
    window = ProjectVisualizer()
    refreshes = []
    monkeypatch.setattr(
        window, "_start_project_columns_load",
        lambda *, force=False: refreshes.append(force),
    )
    try:
        QTest.mouseClick(window.rebuild_vocabulary_btn, Qt.LeftButton)
        process.returncode = 1
        window._poll_vocabulary_cli()

        assert window.rebuild_vocabulary_btn.isEnabled()
        assert window.rebuild_vocabulary_btn.text() == "Index Vocabulary"
        assert window.thumbnail_palettes_btn.isEnabled()
        assert window.index_illustrations_btn.isEnabled()
        assert window.project_browse_btn.isEnabled()
        assert not window._tools_loading_bar._active
        assert not window._vocabulary_poll_timer.isActive()
        assert refreshes == []
        assert messages == [(
            "Vocabulary rebuild failed",
            "Vocabulary rebuild failed in CLI",
        )]
    finally:
        window.close()


def test_thumbnail_palette_tool_runs_both_media_once_then_refreshes(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/current/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import ProjectVisualizer

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

    launches = []

    def fake_start(project_path, media_type, output_stream):
        process = FakeProcess()
        launches.append((project_path, media_type, output_stream, process))
        return process

    monkeypatch.setattr(
        "visualizers.project_visualizer._start_thumbnail_palette_cli",
        fake_start,
    )
    window = ProjectVisualizer()
    refreshes = []
    monkeypatch.setattr(
        window, "_start_project_columns_load",
        lambda *, force=False: refreshes.append(force),
    )
    try:
        QTest.mouseClick(window.thumbnail_palettes_btn, Qt.LeftButton)

        assert [(path, media) for path, media, _output, _proc in launches] == [
            ("/current/project", "movie"),
        ]
        assert not window.thumbnail_palettes_btn.isEnabled()
        assert window.thumbnail_palettes_btn.text() == "indexing"
        assert not window.rebuild_vocabulary_btn.isEnabled()
        assert not window.index_illustrations_btn.isEnabled()
        assert window._tools_loading_bar._active
        assert window._thumbnail_palette_poll_timer.isActive()
        assert not window.project_browse_btn.isEnabled()

        QTest.mouseClick(window.thumbnail_palettes_btn, Qt.LeftButton)
        window._on_thumbnail_palettes()
        assert len(launches) == 1

        launches[0][3].returncode = 0
        window._poll_thumbnail_palette_cli()
        assert [(path, media) for path, media, _output, _proc in launches] == [
            ("/current/project", "movie"),
            ("/current/project", "gameplay"),
        ]
        assert refreshes == []

        launches[1][3].returncode = 0
        window._poll_thumbnail_palette_cli()

        assert window.thumbnail_palettes_btn.isEnabled()
        assert window.thumbnail_palettes_btn.text() == "Index Thumbnails"
        assert window.rebuild_vocabulary_btn.isEnabled()
        assert window.index_illustrations_btn.isEnabled()
        assert not window._tools_loading_bar._active
        assert not window._thumbnail_palette_poll_timer.isActive()
        assert window.project_browse_btn.isEnabled()
        assert refreshes == [True]
    finally:
        window.close()


def test_thumbnail_palette_tool_restores_button_and_surfaces_cli_failure(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/current/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import ProjectVisualizer

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

    process = FakeProcess()
    launches = []

    def fake_start(project_path, media_type, output_stream):
        launches.append((project_path, media_type))
        output_stream.write(b"SAM3 palette build failed")
        return process

    messages = []
    monkeypatch.setattr(
        "visualizers.project_visualizer._start_thumbnail_palette_cli",
        fake_start,
    )
    monkeypatch.setattr(
        "visualizers.project_visualizer.QMessageBox.critical",
        lambda parent, title, message: messages.append((title, message)),
    )
    window = ProjectVisualizer()
    refreshes = []
    monkeypatch.setattr(
        window, "_start_project_columns_load",
        lambda *, force=False: refreshes.append(force),
    )
    try:
        QTest.mouseClick(window.thumbnail_palettes_btn, Qt.LeftButton)
        process.returncode = 1
        window._poll_thumbnail_palette_cli()

        assert launches == [
            ("/current/project", "movie"),
            ("/current/project", "gameplay"),
        ]
        assert not window.thumbnail_palettes_btn.isEnabled()
        assert messages == []

        process.returncode = 0
        window._poll_thumbnail_palette_cli()

        assert window.thumbnail_palettes_btn.isEnabled()
        assert window.thumbnail_palettes_btn.text() == "Index Thumbnails"
        assert window.rebuild_vocabulary_btn.isEnabled()
        assert window.index_illustrations_btn.isEnabled()
        assert not window._tools_loading_bar._active
        assert not window._thumbnail_palette_poll_timer.isActive()
        assert window.project_browse_btn.isEnabled()
        assert refreshes == []
        assert len(messages) == 1
        assert messages[0][0] == "Thumbnail Palettes failed"
        assert "Movie thumbnail palettes failed" in messages[0][1]
        assert "SAM3 palette build failed" in messages[0][1]
    finally:
        window.close()


def test_illustration_tool_indexes_both_media_then_refreshes(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/current/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import ProjectVisualizer

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

    process = FakeProcess()
    launches = []

    def fake_start(project_path, output_stream):
        launches.append((project_path, output_stream))
        return process

    monkeypatch.setattr(
        "visualizers.project_visualizer._start_illustration_cli", fake_start,
    )
    window = ProjectVisualizer()
    refreshes = []
    monkeypatch.setattr(
        window, "_start_project_columns_load",
        lambda *, force=False: refreshes.append(force),
    )
    try:
        QTest.mouseClick(window.index_illustrations_btn, Qt.LeftButton)

        assert len(launches) == 1
        assert launches[0][0] == "/current/project"
        assert not window.index_illustrations_btn.isEnabled()
        assert window.index_illustrations_btn.text() == "indexing"
        assert not window.thumbnail_palettes_btn.isEnabled()
        assert not window.rebuild_vocabulary_btn.isEnabled()
        assert not window.project_browse_btn.isEnabled()
        assert window._tools_loading_bar._active
        assert window._illustration_poll_timer.isActive()

        window._on_index_illustrations()
        window._on_thumbnail_palettes()
        window._on_rebuild_vocabulary()
        assert len(launches) == 1

        process.returncode = 0
        window._poll_illustration_cli()

        assert window.index_illustrations_btn.isEnabled()
        assert window.index_illustrations_btn.text() == "Index Illustrations"
        assert window.thumbnail_palettes_btn.isEnabled()
        assert window.rebuild_vocabulary_btn.isEnabled()
        assert window.project_browse_btn.isEnabled()
        assert not window._tools_loading_bar._active
        assert not window._illustration_poll_timer.isActive()
        assert refreshes == [True]
    finally:
        window.close()


def test_illustration_tool_restores_controls_and_surfaces_cli_failure(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/current/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import ProjectVisualizer

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

    process = FakeProcess()

    def fake_start(_project_path, output_stream):
        output_stream.write(b"Illustration index failed in CLI")
        return process

    messages = []
    monkeypatch.setattr(
        "visualizers.project_visualizer._start_illustration_cli", fake_start,
    )
    monkeypatch.setattr(
        "visualizers.project_visualizer.QMessageBox.critical",
        lambda parent, title, message: messages.append((title, message)),
    )
    window = ProjectVisualizer()
    refreshes = []
    monkeypatch.setattr(
        window, "_start_project_columns_load",
        lambda *, force=False: refreshes.append(force),
    )
    try:
        QTest.mouseClick(window.index_illustrations_btn, Qt.LeftButton)
        process.returncode = 1
        window._poll_illustration_cli()

        assert window.index_illustrations_btn.isEnabled()
        assert window.index_illustrations_btn.text() == "Index Illustrations"
        assert window.thumbnail_palettes_btn.isEnabled()
        assert window.rebuild_vocabulary_btn.isEnabled()
        assert window.project_browse_btn.isEnabled()
        assert not window._tools_loading_bar._active
        assert not window._illustration_poll_timer.isActive()
        assert refreshes == []
        assert messages == [(
            "Illustration indexing failed",
            "Illustration index failed in CLI",
        )]
    finally:
        window.close()


def test_index_all_runs_every_index_in_order_then_refreshes(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/current/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import ProjectVisualizer

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

    launches = []

    def launch(kind, media_type=None):
        process = FakeProcess()
        launches.append((kind, media_type, process))
        return process

    monkeypatch.setattr(
        "visualizers.project_visualizer._start_thumbnail_palette_cli",
        lambda project_path, media_type, output: launch("thumbnail", media_type),
    )
    monkeypatch.setattr(
        "visualizers.project_visualizer._start_vocabulary_cli",
        lambda project_path, output: launch("vocabulary"),
    )
    monkeypatch.setattr(
        "visualizers.project_visualizer._start_illustration_cli",
        lambda project_path, output: launch("illustration"),
    )

    window = ProjectVisualizer()
    refreshes = []
    monkeypatch.setattr(
        window, "_start_project_columns_load",
        lambda *, force=False: refreshes.append(force),
    )
    try:
        QTest.mouseClick(window.index_all_btn, Qt.LeftButton)

        assert [(kind, media) for kind, media, _proc in launches] == [
            ("thumbnail", "movie"),
        ]
        assert window.index_all_btn.text() == "indexing"
        assert not window.index_all_btn.isEnabled()
        assert not window.thumbnail_palettes_btn.isEnabled()
        assert not window.rebuild_vocabulary_btn.isEnabled()
        assert not window.index_illustrations_btn.isEnabled()
        assert not window.project_browse_btn.isEnabled()
        assert window._tools_loading_bar._active
        assert window._index_all_poll_timer.isActive()

        launches[-1][2].returncode = 0
        window._poll_index_all_cli()
        assert [(kind, media) for kind, media, _proc in launches] == [
            ("thumbnail", "movie"),
            ("thumbnail", "gameplay"),
        ]

        launches[-1][2].returncode = 0
        window._poll_index_all_cli()
        assert launches[-1][:2] == ("vocabulary", None)

        launches[-1][2].returncode = 0
        window._poll_index_all_cli()
        assert launches[-1][:2] == ("illustration", None)

        launches[-1][2].returncode = 0
        window._poll_index_all_cli()

        assert window.index_all_btn.text() == "Index"
        assert window.index_all_btn.isEnabled()
        assert window.thumbnail_palettes_btn.isEnabled()
        assert window.rebuild_vocabulary_btn.isEnabled()
        assert window.index_illustrations_btn.isEnabled()
        assert window.project_browse_btn.isEnabled()
        assert not window._tools_loading_bar._active
        assert not window._index_all_poll_timer.isActive()
        assert refreshes == [True]
    finally:
        window.close()


def test_index_all_continues_after_failure_then_reports_without_refresh(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/current/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import ProjectVisualizer

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

    launches = []

    def fake_thumbnail(_project_path, media_type, _output):
        process = FakeProcess()
        launches.append(("thumbnail", media_type, process))
        return process

    def fake_vocabulary(_project_path, output):
        process = FakeProcess()
        output.write(b"Vocabulary failed in CLI")
        launches.append(("vocabulary", None, process))
        return process

    def fake_illustration(_project_path, _output):
        process = FakeProcess()
        launches.append(("illustration", None, process))
        return process

    monkeypatch.setattr(
        "visualizers.project_visualizer._start_thumbnail_palette_cli",
        fake_thumbnail,
    )
    monkeypatch.setattr(
        "visualizers.project_visualizer._start_vocabulary_cli", fake_vocabulary,
    )
    monkeypatch.setattr(
        "visualizers.project_visualizer._start_illustration_cli", fake_illustration,
    )
    messages = []
    monkeypatch.setattr(
        "visualizers.project_visualizer.QMessageBox.critical",
        lambda parent, title, message: messages.append((title, message)),
    )

    window = ProjectVisualizer()
    refreshes = []
    monkeypatch.setattr(
        window, "_start_project_columns_load",
        lambda *, force=False: refreshes.append(force),
    )
    try:
        QTest.mouseClick(window.index_all_btn, Qt.LeftButton)
        launches[-1][2].returncode = 0
        window._poll_index_all_cli()
        launches[-1][2].returncode = 0
        window._poll_index_all_cli()
        launches[-1][2].returncode = 1
        window._poll_index_all_cli()

        assert launches[-1][:2] == ("illustration", None)
        assert window._tools_loading_bar._active

        launches[-1][2].returncode = 0
        window._poll_index_all_cli()

        assert window.index_all_btn.text() == "Index"
        assert window.index_all_btn.isEnabled()
        assert window.thumbnail_palettes_btn.isEnabled()
        assert window.rebuild_vocabulary_btn.isEnabled()
        assert window.index_illustrations_btn.isEnabled()
        assert window.project_browse_btn.isEnabled()
        assert not window._tools_loading_bar._active
        assert not window._index_all_poll_timer.isActive()
        assert refreshes == []
        assert messages == [(
            "Indexing failed",
            "Vocabulary failed:\n\nVocabulary failed in CLI",
        )]
    finally:
        window.close()


def test_project_visualizer_first_show_uses_saved_geometry_while_loading(
    app, fake_prefs, monkeypatch,
):
    from PyQt5.QtCore import QRect
    from PyQt5.QtWidgets import QApplication

    class _DesktopScreen:
        @staticmethod
        def availableGeometry():
            return QRect(0, 0, 1920, 1080)

    fake_prefs["path"] = "/fake/project"
    fake_prefs["window_project"] = [40, 50, 1000, 650, 0, 0]
    monkeypatch.setattr(
        QApplication, "primaryScreen", staticmethod(lambda: _DesktopScreen()),
    )
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        assert not window.isVisible()

        window.show()

        geometry = window.geometry()
        assert (
            geometry.x(), geometry.y(), geometry.width(), geometry.height(),
        ) == (40, 50, 1000, 650)
        assert window._shown_as_project is True
        assert all(
            widget.column.state == "loading"
            for widget in window._project_column_widgets.values()
        )
    finally:
        window.close()


def test_project_visualizer_datavis_body_is_borderless_at_multiple_widths(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        window.show()
        first_widget = window._project_column_widgets[EXPECTED_COLUMN_IDS[0]]
        assert "border:" not in first_widget.styleSheet()
        assert "border: 1px" in first_widget._header_label.styleSheet()
        assert "border-left: 1px" in first_widget._count_label.styleSheet()
        assert "border-right: 1px" in first_widget._count_label.styleSheet()
        assert "border-bottom: 1px" in first_widget._count_label.styleSheet()
        assert "border" not in first_widget._datavis_widget.styleSheet()

        expected_color = QColor(theme.CANVAS_BG)
        for window_width in (900, 1500):
            window.resize(window_width, 700)
            app.processEvents()

            browser = window._browser
            datavis = first_widget._datavis_widget
            body_y = datavis.mapTo(browser, QPoint(0, datavis.height() // 2)).y()
            image = browser.grab().toImage()

            assert image.width() == browser.width()
            assert all(image.pixelColor(x, body_y) == expected_color for x in range(image.width()))
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_project_visualizer_header_count_rows_are_equal_and_datavis_follows(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        window.show()
        widget = window._project_column_widgets["movies"]
        for window_height in (560, 800):
            window.resize(1000, window_height)
            app.processEvents()

            header = widget._header_label.geometry()
            loading_gap = widget._loading_gap.geometry()
            count = widget._count_label.geometry()
            datavis = widget._datavis_widget.geometry()

            assert header.height() == count.height() == 28
            assert loading_gap.height() == theme.INSPECTOR_GAP
            assert loading_gap.top() == header.bottom() + 1
            assert count.top() == loading_gap.bottom() + 1
            assert datavis.top() == count.bottom() + 1
            assert datavis.bottom() == widget.rect().bottom()
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_project_media_datavis_uses_canonical_horizontal_content_gutters(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )

    from visualizers.project_visualizer import (
        _PROJECT_COLUMN_SPACING,
        _project_datavis_horizontal_insets,
        ProjectVisualizer,
    )

    window = ProjectVisualizer()
    try:
        movies = window._project_column_widgets["movies"]
        gameplay = window._project_column_widgets["gameplay"]
        shots = window._project_column_widgets["shots"]
        movies.set_column(ProjectColumn(
            id="movies", title="Movies", count=3,
            datavis=_media_items_datavis(3), state="ready",
        ))
        gameplay.set_column(ProjectColumn(
            id="gameplay", title="Gameplay", count=2,
            datavis=_media_items_datavis(2, "gameplay"), state="ready",
        ))
        window.show()

        left_inset, right_inset = _project_datavis_horizontal_insets()
        assert right_inset + _PROJECT_COLUMN_SPACING + left_inset == theme.INSPECTOR_GAP

        browser = window._browser
        for window_width in (900, 1200, 1500):
            window.resize(window_width, 700)
            app.processEvents()

            movie_cell, next_movie_cell = movies._datavis_widget._media_item_cells[:2]
            gameplay_cell = gameplay._datavis_widget._media_item_cells[0]
            movie_content = movies._datavis_widget._content_widget
            gameplay_content = gameplay._datavis_widget._content_widget
            shots_content = shots._datavis_widget._content_widget

            assert movie_cell.geometry().left() == 0
            assert movie_cell.width() == movie_content.width()
            assert gameplay_cell.geometry().left() == 0
            assert gameplay_cell.width() == gameplay_content.width()
            assert next_movie_cell.y() == movie_cell.geometry().bottom() + 1

            movie_count_bottom = movies._count_label.mapTo(
                browser, movies._count_label.rect().bottomLeft()
            ).y()
            movie_cell_top = movie_cell.mapTo(browser, QPoint(0, 0)).y()
            assert movie_cell_top - movie_count_bottom - 1 == theme.INSPECTOR_GAP

            movie_datavis_left = movies._datavis_widget.mapTo(browser, QPoint(0, 0)).x()
            movie_datavis_right = movie_datavis_left + movies._datavis_widget.width() - 1
            movie_cell_left = movie_cell.mapTo(browser, QPoint(0, 0)).x()
            movie_cell_right = movie_cell_left + movie_cell.width() - 1
            gameplay_cell_left = gameplay_cell.mapTo(browser, QPoint(0, 0)).x()
            gameplay_cell_right = gameplay_cell_left + gameplay_cell.width() - 1
            shots_content_left = shots_content.mapTo(browser, QPoint(0, 0)).x()

            assert movie_cell_left - movie_datavis_left == left_inset
            assert movie_datavis_right - movie_cell_right == right_inset
            assert gameplay_cell_left - movie_cell_right - 1 == theme.INSPECTOR_GAP
            assert shots_content_left - gameplay_cell_right - 1 == theme.INSPECTOR_GAP

            image = browser.grab().toImage()
            sample_y = movie_cell.mapTo(
                browser, QPoint(0, movie_cell.height() // 2)
            ).y()
            gap_pixels = range(movie_cell_right + 1, gameplay_cell_left)
            assert all(
                image.pixelColor(x, sample_y) == QColor(theme.CANVAS_BG)
                for x in gap_pixels
            )
            assert all(
                image.pixelColor(x, sample_y) != QColor(theme.UI_BORDER)
                for x in gap_pixels
            )
            outer_pixels = list(range(movie_datavis_left, movie_cell_left)) + list(
                range(movie_cell_right + 1, movie_datavis_right + 1)
            )
            assert outer_pixels
            assert all(
                image.pixelColor(x, sample_y) == QColor(theme.CANVAS_BG)
                for x in outer_pixels
            )
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_vocabulary_datavis_rounding_fills_exact_available_height():
    from visualizers.project_visualizer import _proportional_heights

    heights = _proportional_heights([1, 1, 1], 10)

    assert heights == [3, 3, 4]
    assert sum(heights) == 10


def test_vocabulary_datavis_minima_redistribute_remaining_height():
    from visualizers.project_visualizer import _proportional_heights

    heights = _proportional_heights([8, 1, 1], 100, minimum_height=20)

    assert heights == [60, 20, 20]
    assert sum(heights) == 100


def test_movies_media_items_render_314_equal_gapless_cells_with_exact_pixel_fill(app):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    datavis = _ProjectDatavisWidget()
    try:
        datavis.resize(140, theme.INSPECTOR_GAP + 314 * 2)
        datavis.set_datavis(_media_items_datavis(314))
        datavis.show()
        app.processEvents()

        cells = datavis._media_item_cells
        assert len(cells) == 314
        assert all(cell.isVisible() for cell in cells)
        assert all(cell.height() == 2 for cell in cells)
        assert cells[0].y() == theme.INSPECTOR_GAP
        assert all(
            cells[index].y() == cells[index - 1].y() + cells[index - 1].height()
            for index in range(1, len(cells))
        )
        assert cells[-1].y() + cells[-1].height() == datavis.height()
        assert all(cell.findChildren(QLabel) == [] for cell in cells)

        image = datavis.grab().toImage()
        for y in range(theme.INSPECTOR_GAP):
            assert image.pixelColor(datavis.width() // 2, y) == QColor(theme.CANVAS_BG)
        colors = (QColor(theme.CELL_BG), QColor(theme.PANEL_BG))
        for index, cell in enumerate(cells):
            for y in range(cell.y(), cell.y() + cell.height()):
                assert image.pixelColor(datavis.width() // 2, y) == colors[index % 2]
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_gameplay_media_items_render_two_equal_contiguous_alternating_cells(app):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    datavis = _ProjectDatavisWidget()
    try:
        datavis.resize(140, theme.INSPECTOR_GAP + 200)
        datavis.set_datavis(_media_items_datavis(2, "gameplay"))
        datavis.show()
        app.processEvents()

        first, second = datavis._media_item_cells
        content = datavis._content_widget
        assert first.geometry().getRect() == (
            0, theme.INSPECTOR_GAP, content.width(), 100,
        )
        assert second.geometry().getRect() == (
            0, theme.INSPECTOR_GAP + 100, content.width(), 100,
        )
        assert second.y() == first.y() + first.height()
        assert second.y() + second.height() == datavis.height()

        image = datavis.grab().toImage()
        x = datavis.width() // 2
        assert image.pixelColor(x, theme.INSPECTOR_GAP - 1) == QColor(theme.CANVAS_BG)
        assert image.pixelColor(x, first.y()) == QColor(theme.CELL_BG)
        assert image.pixelColor(x, first.y() + first.height() - 1) == QColor(theme.CELL_BG)
        assert image.pixelColor(x, second.y()) == QColor(theme.PANEL_BG)
        assert image.pixelColor(x, datavis.height() - 1) == QColor(theme.PANEL_BG)
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_media_items_use_cached_thumbnail_foreground_and_missing_default(app):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    datavis = _ProjectDatavisWidget()
    try:
        payload = _media_items_datavis(2)
        payload["items"][0]["thumbnail_foreground_rgb"] = [12, 34, 56]
        payload["items"][1]["thumbnail_foreground_rgb"] = None
        datavis.resize(140, theme.INSPECTOR_GAP + 200)
        datavis.set_datavis(payload)
        datavis.show()
        app.processEvents()

        first, second = datavis._media_item_cells
        image = datavis.grab().toImage()
        x = datavis.width() // 2
        assert image.pixelColor(x, first.y()) == QColor(12, 34, 56)
        assert image.pixelColor(x, second.y()) == QColor(theme.PANEL_BG)

        replacement = _media_items_datavis(1)
        replacement["items"][0]["thumbnail_foreground_rgb"] = None
        datavis.set_datavis(replacement)
        app.processEvents()

        assert datavis.grab().toImage().pixelColor(
            x, datavis._media_item_cells[0].y()
        ) == QColor(theme.CELL_BG)
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_media_items_one_item_fills_every_pixel_after_canonical_top_gap(app):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    datavis = _ProjectDatavisWidget()
    try:
        datavis.resize(140, 103)
        datavis.set_datavis(_media_items_datavis(1))
        datavis.show()
        app.processEvents()

        cell = datavis._media_item_cells[0]
        assert cell.geometry().getRect() == (
            0, theme.INSPECTOR_GAP, datavis._content_widget.width(), 100,
        )
        assert cell.y() + cell.height() == datavis.height()
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_media_items_zero_hides_cells_and_leaves_canvas_empty(app):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    datavis = _ProjectDatavisWidget()
    try:
        datavis.resize(140, 103)
        datavis.set_datavis(_media_items_datavis(2))
        datavis.set_datavis(_media_items_datavis(0))
        datavis.show()
        app.processEvents()

        assert datavis._media_items == []
        assert not any(cell.isVisible() for cell in datavis._media_item_cells)
        image = datavis.grab().toImage()
        for y in range(datavis.height()):
            assert image.pixelColor(datavis.width() // 2, y) == QColor(theme.CANVAS_BG)
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_media_items_responsive_resize_redistributes_only_rounding_pixels(app):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    datavis = _ProjectDatavisWidget()
    try:
        datavis.set_datavis(_media_items_datavis(3))
        datavis.show()
        for height, expected_heights in (
            (theme.INSPECTOR_GAP + 300, [100, 100, 100]),
            (theme.INSPECTOR_GAP + 301, [101, 100, 100]),
            (theme.INSPECTOR_GAP + 302, [101, 101, 100]),
        ):
            datavis.resize(140, height)
            app.processEvents()

            cells = datavis._media_item_cells
            assert [cell.height() for cell in cells] == expected_heights
            assert cells[0].y() == theme.INSPECTOR_GAP
            assert all(
                cells[index].y() == cells[index - 1].y() + cells[index - 1].height()
                for index in range(1, len(cells))
            )
            assert cells[-1].y() + cells[-1].height() == datavis.height()
            assert max(expected_heights) - min(expected_heights) <= 1
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


@pytest.mark.parametrize(("media_type", "title"), [
    ("movie", "Dodge City"),
    ("gameplay", "Red Dead Redemption 2"),
])
def test_media_item_cell_attaches_identity_and_uses_canonical_tooltip(
    app, monkeypatch, media_type, title,
):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    item = {
        "index": 0,
        "title": title,
        "filename": f"{media_type}-item.mp4",
        "media_type": media_type,
        "media_id": f"{media_type}_stable_id",
    }
    datavis = _ProjectDatavisWidget()
    try:
        datavis.resize(140, 103)
        datavis.set_datavis({"kind": "media_items", "count": 1, "items": [item]})
        datavis.show()
        app.processEvents()

        cell = datavis._media_item_cells[0]
        assert cell.item is item
        assert cell.toolTip() == title

        shown = []
        monkeypatch.setattr(
            QToolTip, "showText",
            lambda pos, text, widget: shown.append((pos, text, widget)),
        )
        QApplication.sendEvent(cell, QEvent(QEvent.Enter))
        assert len(shown) == 1
        assert shown[0][1:] == (title, cell)

        local_pos = cell.rect().center()
        event = QHelpEvent(
            QEvent.ToolTip, local_pos, cell.mapToGlobal(local_pos),
        )
        QApplication.sendEvent(cell, event)
        assert event.isAccepted()
        assert QToolTip.text() == title
    finally:
        QToolTip.hideText()
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_media_item_hover_updates_title_and_datavis_leave_dismisses_popup(
    app, monkeypatch,
):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    datavis = _ProjectDatavisWidget()
    try:
        datavis.resize(140, 203)
        datavis.set_datavis({
            "kind": "media_items",
            "count": 2,
            "items": [
                {
                    "index": 0, "title": "First Movie", "filename": "first.mp4",
                    "media_type": "movie", "media_id": "tmdb_1",
                },
                {
                    "index": 1, "title": "Second Movie", "filename": "second.mp4",
                    "media_type": "movie", "media_id": "tmdb_2",
                },
            ],
        })
        datavis.show()
        app.processEvents()

        first, second = datavis._media_item_cells
        for cell, title in ((first, "First Movie"), (second, "Second Movie")):
            local_pos = cell.rect().center()
            event = QHelpEvent(
                QEvent.ToolTip, local_pos, cell.mapToGlobal(local_pos),
            )
            QApplication.sendEvent(cell, event)
            assert QToolTip.text() == title

        hide_calls = []
        monkeypatch.setattr(QToolTip, "hideText", lambda: hide_calls.append(True))
        QApplication.sendEvent(datavis, QEvent(QEvent.Leave))
        assert hide_calls == [True]
        assert datavis.toolTip() == ""
    finally:
        QToolTip.hideText()
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


@pytest.mark.parametrize(("media_type", "filename"), [
    ("movie", "dodge-city.mp4"),
    ("gameplay", "rdr2-session.mp4"),
])
def test_media_item_double_click_uses_canonical_shotlist_navigation(
    app, fake_prefs, monkeypatch, media_type, filename,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)
    opened = []
    monkeypatch.setattr(
        "visualizers.shot_visualizer.open_at_shot",
        lambda project_path, item_filename, *, media_type: opened.append(
            (project_path, item_filename, media_type)
        ),
    )

    from visualizers.project_visualizer import ProjectVisualizer

    item = {
        "index": 0,
        "title": "Selected Item",
        "filename": filename,
        "media_type": media_type,
        "media_id": f"{media_type}_stable_id",
    }
    window = ProjectVisualizer()
    try:
        window.resize(1000, 700)
        window.show()
        widget = window._project_column_widgets[
            "movies" if media_type == "movie" else "gameplay"
        ]
        widget.set_column(ProjectColumn(
            id=widget.column.id,
            title=widget.column.title,
            count=1,
            datavis={"kind": "media_items", "count": 1, "items": [item]},
            state="ready",
        ))
        app.processEvents()
        cell = widget._datavis_widget._media_item_cells[0]

        QTest.mouseClick(cell, Qt.LeftButton)
        assert opened == []

        QTest.mouseDClick(cell, Qt.LeftButton)
        assert opened == [("/fake/project", filename, media_type)]
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_media_item_double_click_opens_only_the_activated_cell(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)
    opened = []
    monkeypatch.setattr(
        "visualizers.shot_visualizer.open_at_shot",
        lambda project_path, filename, *, media_type: opened.append(filename),
    )

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        window.resize(1000, 700)
        window.show()
        widget = window._project_column_widgets["movies"]
        widget.set_column(ProjectColumn(
            id="movies", title="Movies", count=2,
            datavis={
                "kind": "media_items", "count": 2,
                "items": [
                    {
                        "index": 0, "title": "First", "filename": "first.mp4",
                        "media_type": "movie", "media_id": "tmdb_1",
                    },
                    {
                        "index": 1, "title": "Second", "filename": "second.mp4",
                        "media_type": "movie", "media_id": "tmdb_2",
                    },
                ],
            },
            state="ready",
        ))
        app.processEvents()

        first, second = widget._datavis_widget._media_item_cells
        QTest.mouseDClick(second, Qt.LeftButton)

        assert first.item["filename"] == "first.mp4"
        assert second.item["filename"] == "second.mp4"
        assert opened == ["second.mp4"]
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


@pytest.mark.parametrize(("counts", "height", "expected_heights", "expected_y"), [
    ([3, 1], 199, [144, 49], [3, 150]),
    ([8, 1, 1], 109, [52, 24, 24], [3, 58, 85]),
])
def test_vocabulary_datavis_normal_geometry_is_unchanged(
    app, counts, height, expected_heights, expected_y,
):
    from visualizers.project_visualizer import _VocabularyDatavisWidget

    datavis = _VocabularyDatavisWidget()
    try:
        datavis.resize(140, height)
        datavis.set_datavis({
            "kind": "vocabulary_fields",
            "fields": [
                {"field": f"field-{index}", "count": count}
                for index, count in enumerate(counts)
            ],
        })
        app.processEvents()

        cells = datavis._field_cells
        assert [cell.height() for cell in cells] == expected_heights
        assert [cell.y() for cell in cells] == expected_y
        assert cells[-1].y() + cells[-1].height() == height
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


@pytest.mark.parametrize(("width", "height"), [(140, 199), (60, 98), (140, 7)])
def test_shot_types_match_vocabulary_pixels_and_geometry(app, width, height):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    rows = [("diegetic", 3200), ("title", 20), ("future-mode", 1)]
    vocabulary = _ProjectDatavisWidget()
    shot_types = _ProjectDatavisWidget()
    try:
        for datavis in (vocabulary, shot_types):
            datavis.resize(width, height)
            datavis.show()
        vocabulary.set_datavis({
            "kind": "vocabulary_fields",
            "fields": [{"field": name, "count": count} for name, count in rows],
        })
        shot_types.set_datavis({
            "kind": "shot_types",
            "fields": [
                {"name": name, "count": count, "synthetic": False}
                for name, count in rows
            ],
        })
        app.processEvents()

        vocabulary_cells = vocabulary._field_cells
        shot_type_cells = shot_types._field_cells
        assert [cell.geometry() for cell in shot_type_cells] == [
            cell.geometry() for cell in vocabulary_cells
        ]
        assert [cell.field_label.text() for cell in shot_type_cells] == [
            cell.field_label.text() for cell in vocabulary_cells
        ]
        assert [cell.count_label.text() for cell in shot_type_cells] == [
            cell.count_label.text() for cell in vocabulary_cells
        ]
        assert [cell.styleSheet() for cell in shot_type_cells] == [
            cell.styleSheet() for cell in vocabulary_cells
        ]
        assert shot_types.grab().toImage() == vocabulary.grab().toImage()
    finally:
        for datavis in (vocabulary, shot_types):
            datavis.close()
            datavis.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("kind", ["silhouette_fields", "engraving_fields"])
@pytest.mark.parametrize(("width", "height"), [(140, 199), (60, 98), (140, 7)])
def test_indexed_field_kinds_match_vocabulary_pixels_and_geometry(
    app, kind, width, height,
):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    rows = [("animals", 3200), ("objects", 20), ("wearing", 1)]
    vocabulary = _ProjectDatavisWidget()
    indexed = _ProjectDatavisWidget()
    try:
        for datavis in (vocabulary, indexed):
            datavis.resize(width, height)
            datavis.show()
        vocabulary.set_datavis({
            "kind": "vocabulary_fields",
            "fields": [{"field": name, "count": count} for name, count in rows],
        })
        indexed.set_datavis({
            "kind": kind,
            "fields": [{"field": name, "count": count} for name, count in rows],
        })
        app.processEvents()

        vocabulary_cells = vocabulary._field_cells
        indexed_cells = indexed._field_cells
        assert [cell.geometry() for cell in indexed_cells] == [
            cell.geometry() for cell in vocabulary_cells
        ]
        assert [cell.field_label.text() for cell in indexed_cells] == [
            cell.field_label.text() for cell in vocabulary_cells
        ]
        assert [cell.count_label.text() for cell in indexed_cells] == [
            cell.count_label.text() for cell in vocabulary_cells
        ]
        assert [cell.styleSheet() for cell in indexed_cells] == [
            cell.styleSheet() for cell in vocabulary_cells
        ]
        assert indexed.findChildren(QScrollArea) == []
        assert indexed.grab().toImage() == vocabulary.grab().toImage()
    finally:
        for datavis in (vocabulary, indexed):
            datavis.close()
            datavis.deleteLater()
        app.processEvents()


def test_shot_types_resize_proportionally_with_canonical_gaps_and_compact_counts(app):
    from visualizers.project_visualizer import _COLUMN_ROW_H, _ProjectColumnWidget

    widget = _ProjectColumnWidget(ProjectColumn(
        id="shots", title="Shots", count=1_003_201,
        datavis={
            "kind": "shot_types",
            "fields": [
                {"name": "diegetic", "count": 1_000_000, "synthetic": False},
                {"name": "<untyped>", "count": 3200, "synthetic": True},
                {"name": "future-mode", "count": 1, "synthetic": False},
            ],
        },
        state="ready",
    ))
    try:
        widget.show()
        previous_heights = None
        for widget_height in (258, 458):
            widget.resize(140, widget_height)
            app.processEvents()

            datavis = widget._datavis_widget
            cells = datavis._field_cells
            heights = [cell.height() for cell in cells]
            assert datavis.height() == widget_height - 2 * _COLUMN_ROW_H - theme.INSPECTOR_GAP
            assert sum(heights) + len(cells) * theme.INSPECTOR_GAP == datavis.height()
            assert cells[0].y() == theme.INSPECTOR_GAP
            assert all(
                cells[index].y()
                == cells[index - 1].geometry().bottom() + 1 + theme.INSPECTOR_GAP
                for index in range(1, len(cells))
            )
            assert cells[-1].geometry().bottom() == datavis.rect().bottom()
            if previous_heights is not None:
                assert heights != previous_heights
            previous_heights = heights

        assert [cell.field_label.text() for cell in cells] == [
            "diegetic", "<untyped>", "future-mode",
        ]
        assert [cell.count_label.text() for cell in cells] == ["1M", "3.2k", "1"]
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_shot_types_narrow_column_keeps_untyped_label_and_count(app):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    datavis = _ProjectDatavisWidget()
    try:
        datavis.resize(113, 600)
        datavis.set_datavis({
            "kind": "shot_types",
            "fields": [
                {"name": "diegetic", "count": 274464, "synthetic": False},
                {"name": "<untyped>", "count": 7288, "synthetic": True},
                {"name": "graphics", "count": 1764, "synthetic": False},
            ],
        })
        datavis.show()
        app.processEvents()

        untyped = datavis._field_cells[1]
        assert untyped.field_label.text() == "<untyped>"
        assert untyped.count_label.text() == "7.3k"
        assert untyped.count_label.geometry().right() == datavis.rect().right() - 1

        for width in (140, 113, 100, 113):
            datavis.resize(width, 600)
            app.processEvents()
        assert untyped.field_label.text() == "<untyped>"
        assert untyped.count_label.text() == "7.3k"
        assert untyped.count_label.geometry().right() == datavis.rect().right() - 1
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_shot_types_synthetic_untyped_cell_uses_warning_without_geometry_change(app):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    datavis = _ProjectDatavisWidget()
    try:
        datavis.resize(140, 100)
        datavis.set_datavis({
            "kind": "shot_types",
            "fields": [
                {"name": "<untyped>", "count": 1, "synthetic": True},
                {"name": "untyped", "count": 1, "synthetic": False},
            ],
        })
        datavis.show()
        app.processEvents()

        synthetic, literal = datavis._field_cells
        assert synthetic.field_label.text() == "<untyped>"
        assert literal.field_label.text() == "untyped"
        assert f"background: {theme.WARNING_COLOR};" in synthetic.styleSheet()
        assert f"background: {theme.WARNING_COLOR};" in synthetic.field_label.styleSheet()
        assert f"background: {theme.WARNING_COLOR};" in synthetic.count_label.styleSheet()
        assert f"color: {theme.TEXT};" in literal.field_label.styleSheet()
        assert theme.WARNING_COLOR not in literal.styleSheet()
        assert theme.WARNING_COLOR not in literal.field_label.styleSheet()
        assert theme.WARNING_COLOR not in literal.count_label.styleSheet()
        assert synthetic.geometry().size() == literal.geometry().size()
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_only_synthetic_untyped_cell_emits_audit_activation(app):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    datavis = _ProjectDatavisWidget()
    activated = []
    datavis.syntheticActivated.connect(activated.append)
    try:
        datavis.resize(140, 100)
        datavis.set_datavis({
            "kind": "shot_types",
            "fields": [
                {"name": "<untyped>", "count": 1, "synthetic": True},
                {"name": "untyped", "count": 1, "synthetic": False},
            ],
        })
        datavis.show()
        app.processEvents()

        synthetic, literal = datavis._field_cells
        QTest.mouseDClick(
            datavis, Qt.LeftButton, Qt.NoModifier, literal.geometry().center(),
        )
        assert activated == []

        QTest.mouseDClick(
            datavis, Qt.LeftButton, Qt.NoModifier, synthetic.geometry().center(),
        )
        assert activated == ["shot_types"]
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_project_maps_untyped_datavis_kinds_to_cli_sources(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/current/project"
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: None,
    )
    launches = []
    monkeypatch.setattr(
        "visualizers.project_visualizer._start_untyped_audit_cli",
        lambda project_path, source: launches.append((project_path, source)),
    )

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        window._open_untyped_audit("shot_types")
        window._open_untyped_audit("silhouette_fields")
        window._open_untyped_audit("vocabulary_fields")

        assert launches == [
            ("/current/project", "shot"),
            ("/current/project", "silhouettes"),
        ]
    finally:
        window.close()


def test_shot_types_extreme_density_is_contained_and_display_only(app):
    from visualizers.project_visualizer import _ProjectDatavisWidget

    activated = []
    datavis = _ProjectDatavisWidget()
    datavis.mediaItemActivated.connect(activated.append)
    try:
        datavis.resize(140, 7)
        datavis.set_datavis({
            "kind": "shot_types",
            "fields": [
                {"name": f"type-{index}", "count": 1, "synthetic": False}
                for index in range(100)
            ],
        })
        datavis.show()
        app.processEvents()

        cells = datavis._field_cells
        assert len(cells) == 100
        assert all(cell.height() == 0 for cell in cells)
        assert all(
            0 <= cell.y() <= cell.y() + cell.height() <= datavis.height()
            for cell in cells
        )
        assert cells[-1].y() + cells[-1].height() == datavis.height()
        assert datavis.findChildren(QScrollArea) == []
        assert all(cell.testAttribute(Qt.WA_TransparentForMouseEvents) for cell in cells)
        assert all(cell.toolTip() == "" and not hasattr(cell, "activated") for cell in cells)
        QTest.mouseDClick(cells[0], Qt.LeftButton)
        assert activated == []
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


@pytest.mark.parametrize(("height", "expected_y"), [
    (4 * theme.INSPECTOR_GAP, [3, 6, 9, 12]),
    (4 * theme.INSPECTOR_GAP - 1, [2, 5, 8, 11]),
])
def test_vocabulary_datavis_gap_capacity_boundary_is_contained(
    app, height, expected_y,
):
    from visualizers.project_visualizer import _VocabularyDatavisWidget

    datavis = _VocabularyDatavisWidget()
    try:
        datavis.resize(140, height)
        datavis.set_datavis({
            "kind": "vocabulary_fields",
            "fields": [
                {"field": f"field-{index}", "count": 1}
                for index in range(4)
            ],
        })
        app.processEvents()

        cells = datavis._field_cells
        assert [cell.y() for cell in cells] == expected_y
        assert all(cell.height() == 0 for cell in cells)
        assert all(
            0 <= cell.y() <= cell.y() + cell.height() <= datavis.height()
            for cell in cells
        )
        assert cells[-1].y() + cells[-1].height() == datavis.height()
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_vocabulary_datavis_very_large_field_count_compresses_gaps_within_bounds(app):
    from visualizers.project_visualizer import _VocabularyDatavisWidget

    datavis = _VocabularyDatavisWidget()
    try:
        datavis.resize(140, 7)
        datavis.set_datavis({
            "kind": "vocabulary_fields",
            "fields": [
                {"field": f"field-{index}", "count": 1}
                for index in range(100)
            ],
        })
        app.processEvents()

        cells = datavis._field_cells
        gap_heights = [cells[0].y()] + [
            cells[index].y() - (
                cells[index - 1].y() + cells[index - 1].height()
            )
            for index in range(1, len(cells))
        ]
        assert len(cells) == 100
        assert all(cell.height() == 0 for cell in cells)
        assert all(0 <= gap <= theme.INSPECTOR_GAP for gap in gap_heights)
        assert sum(gap_heights) == datavis.height()
        assert all(
            0 <= cell.y() <= cell.y() + cell.height() <= datavis.height()
            for cell in cells
        )
        assert cells[-1].y() + cells[-1].height() == datavis.height()
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_vocabulary_datavis_zero_height_has_no_negative_or_overflowing_geometry(app):
    from visualizers.project_visualizer import _VocabularyDatavisWidget

    datavis = _VocabularyDatavisWidget()
    try:
        datavis.resize(140, 0)
        datavis.set_datavis({
            "kind": "vocabulary_fields",
            "fields": [
                {"field": f"field-{index}", "count": 1}
                for index in range(10)
            ],
        })
        app.processEvents()

        assert datavis.height() == 0
        assert all(
            cell.y() == 0 and cell.height() == 0
            for cell in datavis._field_cells
        )
        assert all(
            cell.y() + cell.height() <= datavis.height()
            for cell in datavis._field_cells
        )
    finally:
        datavis.close()
        datavis.deleteLater()
        app.processEvents()


def test_vocabulary_datavis_cells_resize_proportionally_and_render_labels(app):
    from visualizers.project_visualizer import _COLUMN_ROW_H, _ProjectColumnWidget

    column = ProjectColumn(
        id="vocabulary", title="Vocabulary", count=4,
        datavis={
            "kind": "vocabulary_fields",
            "fields": [
                {"field": "objects", "count": 3},
                {"field": "settings", "count": 1},
            ],
        },
        state="ready",
    )
    widget = _ProjectColumnWidget(column)
    try:
        widget.show()
        cells = widget._datavis_widget._field_cells
        for widget_height, expected_heights in ((258, [144, 49]), (458, [294, 99])):
            widget.resize(140, widget_height)
            app.processEvents()

            datavis = widget._datavis_widget
            assert datavis.height() == widget_height - 2 * _COLUMN_ROW_H - theme.INSPECTOR_GAP
            assert [cell.height() for cell in cells] == expected_heights
            assert cells[0].y() == theme.INSPECTOR_GAP
            assert cells[1].y() == (
                cells[0].y() + cells[0].height() + theme.INSPECTOR_GAP
            )
            assert (
                sum(cell.height() for cell in cells)
                + len(cells) * theme.INSPECTOR_GAP
                == datavis.height()
            )
            assert cells[-1].geometry().bottom() == datavis.rect().bottom()
            assert cells[0].field_label.text() == "objects"
            assert cells[1].field_label.text() == "settings"
            assert cells[0].count_label.text() == "3"
            assert cells[1].count_label.text() == "1"
            assert f"color: {theme.TEXT_DIM}" in cells[0].count_label.styleSheet()
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_vocabulary_datavis_enforces_readable_minimum_and_formats_exact_count(app):
    from visualizers.project_visualizer import _ProjectColumnWidget

    column = ProjectColumn(
        id="vocabulary", title="Vocabulary", count=100,
        datavis={
            "kind": "vocabulary_fields",
            "fields": [
                {"field": "dominant", "count": 3200},
                {"field": "tiny", "count": 1},
            ],
        },
        state="ready",
    )
    widget = _ProjectColumnWidget(column)
    try:
        widget.resize(140, 258)
        widget.show()
        app.processEvents()

        cells = widget._datavis_widget._field_cells
        minimum = widget._datavis_widget._minimum_cell_height
        assert [cell.height() for cell in cells] == [
            widget._datavis_widget.height() - 2 * theme.INSPECTOR_GAP - minimum,
            minimum,
        ]
        assert cells[-1].geometry().bottom() == widget._datavis_widget.rect().bottom()
        assert cells[0].field_label.text() == "dominant"
        assert cells[0].count_label.text() == "3.2k"
        assert cells[1].field_label.text() == "tiny"
        assert cells[1].count_label.text() == "1"

        widget.resize(140, 98)
        app.processEvents()
        assert [cell.height() for cell in cells] == [32, 1]
        assert cells[-1].geometry().bottom() == widget._datavis_widget.rect().bottom()
        assert cells[0].field_label.text() == "dominant"
        assert cells[0].count_label.text() == "3.2k"
        assert cells[1].field_label.text() == ""
        assert cells[1].count_label.text() == ""
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_vocabulary_datavis_impossible_density_stays_contained_without_scroll(app):
    from visualizers.project_visualizer import _ProjectColumnWidget

    fields = [
        {"field": f"field-{index}", "count": 1}
        for index in range(20)
    ]
    widget = _ProjectColumnWidget(ProjectColumn(
        id="vocabulary", title="Vocabulary", count=20,
        datavis={"kind": "vocabulary_fields", "fields": fields},
        state="ready",
    ))
    try:
        widget.resize(140, 158)
        widget.show()
        app.processEvents()

        datavis = widget._datavis_widget
        cells = datavis._field_cells
        total_gap_height = len(cells) * theme.INSPECTOR_GAP
        assert sum(cell.height() for cell in cells) + total_gap_height == datavis.height() == 99
        assert cells[0].y() == theme.INSPECTOR_GAP
        assert all(
            cells[index].y() == (
                cells[index - 1].geometry().bottom() + 1 + theme.INSPECTOR_GAP
            )
            for index in range(1, len(cells))
        )
        assert cells[-1].geometry().bottom() == datavis.rect().bottom()
        assert all(cell.field_label.text() == "" for cell in cells)
        assert all(cell.count_label.text() == "" for cell in cells)
        assert datavis.findChildren(QScrollArea) == []
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_vocabulary_datavis_suppresses_a_line_that_cannot_fit_its_width(app):
    from visualizers.project_visualizer import _ProjectColumnWidget

    widget = _ProjectColumnWidget(ProjectColumn(
        id="vocabulary", title="Vocabulary", count=1,
        datavis={
            "kind": "vocabulary_fields",
            "fields": [{"field": "a-very-long-vocabulary-field", "count": 1234}],
        },
        state="ready",
    ))
    try:
        widget.resize(60, 258)
        widget.show()
        app.processEvents()

        cell = widget._datavis_widget._field_cells[0]
        assert cell.y() == theme.INSPECTOR_GAP
        assert cell.height() + theme.INSPECTOR_GAP == widget._datavis_widget.height()
        assert cell.field_label.text() == ""
        assert cell.count_label.text() == ""
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_vocabulary_datavis_zero_is_empty(app):
    from visualizers.project_visualizer import _ProjectColumnWidget

    widget = _ProjectColumnWidget(ProjectColumn(
        id="vocabulary", title="Vocabulary", count=1,
        datavis={
            "kind": "vocabulary_fields",
            "fields": [{"field": "objects", "count": 1}],
        },
        state="ready",
    ))
    try:
        cells = widget._datavis_widget._field_cells

        widget.set_column(ProjectColumn(
            id="vocabulary", title="Vocabulary", count=0,
            datavis={"kind": "vocabulary_fields", "fields": []}, state="ready",
        ))
        assert widget._count_label.text() == "0"
        assert widget._datavis_widget._fields == []
        assert not any(cell.isVisible() for cell in cells)
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_project_vocabulary_uses_canonical_inspector_colors(app):
    from visualizers.project_visualizer import _ProjectColumnWidget

    widget = _ProjectColumnWidget(ProjectColumn(
        id="vocabulary", title="Vocabulary", count=1,
        datavis={
            "kind": "vocabulary_fields",
            "fields": [{"field": "objects", "count": 1}],
        },
        state="ready",
    ))
    try:
        widget.resize(140, 258)
        widget.show()
        app.processEvents()
        image = widget.grab().toImage()

        def background_color(child):
            point = child.mapTo(widget, QPoint(5, 5))
            return image.pixelColor(point)

        assert background_color(widget._header_label) == QColor(theme.TITLE_BG)
        assert background_color(widget._count_label) == QColor(theme.PANEL_BG)
        cell = widget._datavis_widget._field_cells[0]
        assert background_color(cell) == QColor(theme.CELL_BG)
        assert background_color(cell.count_label) == QColor(theme.CELL_BG)
        assert f"color: {theme.TEXT_DIM}" in cell.count_label.styleSheet()
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_project_vocabulary_reuses_canonical_inspector_cell_geometry_and_font(app):
    from visualizers.components.metadata_block import (
        INSPECTOR_ROW_HEIGHT,
        table_ui_cell_style,
    )
    from visualizers.project_visualizer import (
        _LauncherButton,
        _PROJECT_ROW_SEAM_H,
        _ProjectColumnWidget,
        _project_row_side_style,
    )

    widget = _ProjectColumnWidget(ProjectColumn(
        id="vocabulary", title="Vocabulary", count=2,
        datavis={
            "kind": "vocabulary_fields",
            "fields": [
                {"field": "objects", "count": 1},
                {"field": "settings", "count": 1},
            ],
        },
        state="ready",
    ))
    reference = QLabel("Inspector cell")
    launcher = _LauncherButton("Metadata", "F2")
    try:
        widget.resize(140, 258)
        widget.show()
        cells = widget._datavis_widget._field_cells
        reference.setStyleSheet(table_ui_cell_style(
            "", "", include_minimum_height=False,
        ))
        reference.show()
        launcher.show()
        app.processEvents()

        assert widget._datavis_widget._minimum_cell_height == INSPECTOR_ROW_HEIGHT
        assert cells[0].field_label.styleSheet() == reference.styleSheet()
        assert cells[0].field_label.font().family() == reference.font().family() == theme.FAMILY_UI
        assert cells[0].field_label.font().pointSize() == reference.font().pointSize() == theme.BASE_PT
        assert cells[0].field_label.font().weight() == reference.font().weight()
        assert cells[0].count_label.font() == cells[0].field_label.font()
        assert f"color: {theme.TEXT_DIM}" in launcher._shortcut_label.styleSheet()
        assert f"color: {theme.TEXT_DIM}" in cells[0].count_label.styleSheet()
        assert cells[0].field_label.alignment() == Qt.AlignLeft | Qt.AlignVCenter
        assert cells[0].count_label.alignment() == Qt.AlignRight | Qt.AlignVCenter
        cell_margins = cells[0].layout().contentsMargins()
        launcher_margins = launcher.layout().contentsMargins()
        assert cell_margins.left() == launcher_margins.left()
        assert cell_margins.top() == launcher_margins.top()
        assert cell_margins.bottom() == launcher_margins.bottom()
        assert cell_margins.right() == _PROJECT_ROW_SEAM_H
        assert cells[0].layout().spacing() == launcher.layout().spacing()
        assert cells[0].rect() == cells[0].contentsRect()
        assert widget._count_label.rect() == widget._count_label.contentsRect()
        assert _project_row_side_style() in cells[0].styleSheet()
        assert "border-bottom" not in cells[0].styleSheet()
        assert "border: none" in cells[0].field_label.styleSheet()
        assert "border: none" in cells[0].count_label.styleSheet()
        assert cells[0].field_label.contentsRect().left() == 0
        assert cells[0].field_label.geometry().right() < cells[0].count_label.geometry().left()
        assert cells[0].count_label.geometry().right() == (
            cells[0].rect().right() - _PROJECT_ROW_SEAM_H
        )
        assert cells[0].count_label.width() == cells[1].count_label.width()
        assert cells[1].y() == (
            cells[0].geometry().bottom() + 1 + theme.INSPECTOR_GAP
        )
    finally:
        launcher.close()
        launcher.deleteLater()
        reference.close()
        reference.deleteLater()
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_project_vocabulary_header_count_and_datavis_use_full_column_width(app):
    from visualizers.project_visualizer import _ProjectColumnWidget

    widget = _ProjectColumnWidget(ProjectColumn(
        id="vocabulary", title="Vocabulary", count=1,
        datavis={
            "kind": "vocabulary_fields",
            "fields": [{"field": "objects", "count": 1}],
        },
        state="ready",
    ))
    try:
        for width in (90, 140, 240):
            widget.resize(width, 258)
            widget.show()
            app.processEvents()

            cell = widget._datavis_widget._field_cells[0]
            assert widget._header_label.geometry().left() == 0
            assert widget._count_label.geometry().left() == 0
            assert widget._datavis_widget.geometry().left() == 0
            assert cell.geometry().left() == 0
            assert {
                widget._header_label.width(),
                widget._count_label.width(),
                widget._datavis_widget.width(),
                cell.width(),
            } == {width}
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_project_vocabulary_rows_use_one_full_width_canonical_gap(app):
    from visualizers.project_visualizer import (
        _COLUMN_ROW_H,
        _PROJECT_ROW_SEAM_H,
        _ProjectColumnWidget,
    )

    widget = _ProjectColumnWidget(ProjectColumn(
        id="vocabulary", title="Vocabulary", count=2,
        datavis={
            "kind": "vocabulary_fields",
            "fields": [
                {"field": "objects", "count": 1},
                {"field": "settings", "count": 1},
            ],
        },
        state="ready",
    ))
    try:
        widget.resize(140, 258)
        widget.show()
        app.processEvents()

        datavis = widget._datavis_widget
        first, last = datavis._field_cells
        assert last.y() == (
            first.geometry().bottom() + 1 + theme.INSPECTOR_GAP
        )
        assert "border-bottom" not in first.styleSheet()
        assert f"border: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER}" in widget._header_label.styleSheet()
        assert f"border-bottom: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER}" in widget._count_border_style
        assert f"border-bottom: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER}" in widget._count_label.styleSheet()
        assert f"border-left: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER}" in widget._count_label.styleSheet()
        assert f"border-right: {_PROJECT_ROW_SEAM_H}px solid {theme.UI_BORDER}" in widget._count_label.styleSheet()
        assert widget._count_label.height() == _COLUMN_ROW_H
        assert (
            sum(cell.height() for cell in (first, last))
            + 2 * theme.INSPECTOR_GAP
            == datavis.height()
        )

        image = datavis.grab().toImage()
        assert first.y() == theme.INSPECTOR_GAP
        for y in range(theme.INSPECTOR_GAP):
            for x in (0, 1, 8, datavis.width() // 2, datavis.width() - 2, datavis.width() - 1):
                assert image.pixelColor(x, y) == QColor(theme.CANVAS_BG)
        assert image.pixelColor(2, theme.INSPECTOR_GAP) == QColor(theme.CELL_BG)
        assert image.pixelColor(datavis.width() - 2, theme.INSPECTOR_GAP) == QColor(theme.CELL_BG)
        gap_top = first.geometry().bottom() + 1
        gap_bottom = last.y() - 1
        assert gap_bottom - gap_top + 1 == theme.INSPECTOR_GAP
        assert image.pixelColor(2, gap_top - 1) == QColor(theme.CELL_BG)
        for y in range(gap_top, gap_bottom + 1):
            for x in (0, 1, 8, datavis.width() // 2, datavis.width() - 2, datavis.width() - 1):
                assert image.pixelColor(x, y) == QColor(theme.CANVAS_BG)
        assert image.pixelColor(2, gap_bottom + 1) == QColor(theme.CELL_BG)
        assert image.pixelColor(datavis.width() - 2, gap_top - 1) == QColor(theme.CELL_BG)
        assert image.pixelColor(datavis.width() - 2, gap_bottom + 1) == QColor(theme.CELL_BG)
        assert image.pixelColor(2, datavis.height() - 1) == QColor(theme.CELL_BG)

        column_image = widget.grab().toImage()
        header_gap_top = widget._header_label.geometry().bottom() + 1
        assert widget._loading_gap.geometry().getRect() == (
            0, header_gap_top, widget.width(), theme.INSPECTOR_GAP,
        )
        for y in range(header_gap_top, header_gap_top + theme.INSPECTOR_GAP):
            for x in (0, 1, 8, widget.width() // 2, widget.width() - 2, widget.width() - 1):
                assert column_image.pixelColor(x, y) == QColor(theme.CANVAS_BG)
        assert widget._count_label.geometry().top() == header_gap_top + theme.INSPECTOR_GAP

        datavis_top = datavis.geometry().top()
        assert datavis_top == widget._count_label.geometry().bottom() + 1
        assert column_image.pixelColor(2, datavis_top - 1) == QColor(theme.UI_BORDER)
        assert column_image.pixelColor(widget.width() // 2, datavis_top - 1) == QColor(theme.UI_BORDER)
        assert column_image.pixelColor(widget.width() - 2, datavis_top - 1) == QColor(theme.UI_BORDER)
        for y in range(datavis_top, datavis_top + theme.INSPECTOR_GAP):
            for x in (0, 1, 8, widget.width() // 2, widget.width() - 2, widget.width() - 1):
                assert column_image.pixelColor(x, y) == QColor(theme.CANVAS_BG)
        assert column_image.pixelColor(2, datavis_top + theme.INSPECTOR_GAP) == QColor(theme.CELL_BG)
        assert column_image.pixelColor(
            widget.width() - 2, datavis_top + theme.INSPECTOR_GAP,
        ) == QColor(theme.CELL_BG)
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_project_vocabulary_count_matches_other_project_counts(app):
    from visualizers.project_visualizer import _ProjectColumnWidget

    vocabulary = _ProjectColumnWidget(ProjectColumn(
        id="vocabulary", title="Vocabulary", count=8400,
        datavis={
            "kind": "vocabulary_fields",
            "fields": [{"field": "objects", "count": 8400}],
        },
        state="ready",
    ))
    movies = _ProjectColumnWidget(ProjectColumn(
        id="movies", title="Movies", count=8400,
        datavis={"kind": "empty"}, state="ready",
    ))
    try:
        for widget in (vocabulary, movies):
            widget.resize(140, 258)
            widget.show()
        app.processEvents()

        vocab_count = vocabulary._count_label
        movies_count = movies._count_label
        assert vocab_count.geometry() == movies_count.geometry()
        assert vocab_count.rect() == movies_count.rect()
        assert vocab_count.contentsRect() == movies_count.contentsRect()
        assert vocab_count.minimumHeight() == movies_count.minimumHeight()
        assert vocab_count.maximumHeight() == movies_count.maximumHeight()
        assert vocab_count.font() == movies_count.font()
        assert vocab_count.styleSheet() == movies_count.styleSheet()
        assert vocab_count.text() == movies_count.text() == "8.4k"
    finally:
        for widget in (vocabulary, movies):
            widget.close()
            widget.deleteLater()
        app.processEvents()


def test_project_vocabulary_count_width_uses_styled_label_size_hint(app):
    from visualizers.project_visualizer import _ProjectColumnWidget

    widget = _ProjectColumnWidget(ProjectColumn(
        id="vocabulary", title="Vocabulary", count=2433,
        datavis={
            "kind": "vocabulary_fields",
            "fields": [{"field": "action", "count": 2433}],
        },
        state="ready",
    ))
    try:
        widget.resize(140, 258)
        widget.show()
        app.processEvents()

        count_label = widget._datavis_widget._field_cells[0].count_label
        assert count_label.text() == "2.4k"
        assert count_label.width() >= count_label.sizeHint().width()
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_project_visualizer_splitter_has_no_extra_browser_inspector_gap(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        window.show()
        for window_width in (900, 1200, 1500):
            window.resize(window_width, 700)
            app.processEvents()

            splitter = window._splitter
            browser = window._browser
            inspector = window._inspector_shell
            handle = splitter.handle(1)

            browser_right = browser.mapTo(splitter, QPoint(0, 0)).x() + browser.width()
            handle_left = handle.mapTo(splitter, QPoint(0, 0)).x()
            handle_right = handle_left + handle.width()
            inspector_left = inspector.mapTo(splitter, QPoint(0, 0)).x()

            assert handle.width() == splitter.handleWidth()
            assert browser_right == handle_left
            assert handle_right == inspector_left
            assert inspector_left - browser_right == splitter.handleWidth()
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_project_visualizer_construction_starts_exactly_one_initial_worker(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    starts = []
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: starts.append(self.generation),
    )

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        assert starts == [1]
        assert window._project_load_generation == 1
        assert window._project_load_state == "loading"
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_project_visualizer_show_does_not_restart_running_initial_load(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    release_worker = threading.Event()
    worker_started = threading.Event()

    def blocked_live_columns(_project_path):
        worker_started.set()
        assert release_worker.wait(5), "test did not release Project worker"
        return [
            ProjectColumn(
                id=col_id, title=title, count=1,
                datavis={"kind": "empty"}, state="ready",
            )
            for col_id, title in PROJECT_COLUMN_IDS_AND_TITLES
            if col_id in ("movies", "gameplay", "silhouettes", "engravings")
        ]

    monkeypatch.setattr(corpus_stats_mod, "get_live_project_columns", blocked_live_columns)
    monkeypatch.setattr(
        corpus_stats_mod,
        "get_cached_project_columns",
        lambda _project_path: [
            ProjectColumn(
                id=col_id, title=title, count=1,
                datavis={"kind": "empty"}, state="ready",
            )
            for col_id, title in PROJECT_COLUMN_IDS_AND_TITLES
            if col_id in ("shots", "vocabulary")
        ],
    )

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        assert worker_started.wait(1), "Project worker did not start"
        worker = window._project_columns_worker
        assert worker.isRunning()

        window.show()
        app.processEvents()

        assert window._project_columns_worker is worker
        assert window._project_load_generation == 1
        assert window._project_load_state == "loading"
        assert all(
            widget._loading_bar._active
            for widget in window._project_column_widgets.values()
        )

        release_worker.set()
        assert worker.wait(5000), "Project worker did not finish"
        app.processEvents()
        assert window._project_load_state == "loaded"
    finally:
        release_worker.set()
        window.close()
        window.deleteLater()
        app.processEvents()


def test_project_visualizer_show_hide_does_not_restart_loaded_initial_load(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    starts = []
    monkeypatch.setattr(
        "visualizers.project_visualizer._ProjectColumnsWorker.start",
        lambda self: starts.append(self.generation),
    )

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        window._on_columns_tier_ready(window._project_load_generation, [
            ProjectColumn(
                id=col_id, title=title, count=1,
                datavis={"kind": "empty"}, state="ready",
            )
            for col_id, title in PROJECT_COLUMN_IDS_AND_TITLES
        ])
        assert window._project_load_state == "loaded"

        window.show()
        app.processEvents()
        window.hide()
        app.processEvents()
        window.show()
        app.processEvents()

        assert starts == [1]
        assert window._project_load_generation == 1
        assert window._project_load_state == "loaded"
        assert all(
            widget.column.state == "ready"
            for widget in window._project_column_widgets.values()
        )
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_project_columns_worker_emits_generation_with_both_tiers(app, monkeypatch):
    from visualizers.project_visualizer import _ProjectColumnsWorker

    live_columns = [
        ProjectColumn(
            id="movies", title="Movies", count=12,
            datavis={"kind": "empty"}, state="ready",
        ),
    ]
    cached_columns = [
        ProjectColumn(
            id="vocabulary", title="Vocabulary", count=1832,
            datavis={"kind": "empty"}, state="ready",
        ),
    ]
    monkeypatch.setattr(corpus_stats_mod, "get_live_project_columns", lambda _path: live_columns)
    monkeypatch.setattr(corpus_stats_mod, "get_cached_project_columns", lambda _path: cached_columns)

    results = []
    worker = _ProjectColumnsWorker("/fake/project", generation=17)
    worker.tier_ready.connect(
        lambda generation, columns: results.append((generation, columns))
    )

    worker.run()

    assert results == [(17, live_columns), (17, cached_columns)]


def test_project_columns_worker_reports_live_failure_and_still_attempts_cached(
    app, monkeypatch,
):
    from visualizers.project_visualizer import _ProjectColumnsWorker

    cached_columns = [
        ProjectColumn(
            id="vocabulary", title="Vocabulary", count=1832,
            datavis={"kind": "empty"}, state="ready",
        ),
    ]

    def fail_live(_path):
        raise RuntimeError("live failed")

    monkeypatch.setattr(corpus_stats_mod, "get_live_project_columns", fail_live)
    monkeypatch.setattr(corpus_stats_mod, "get_cached_project_columns", lambda _path: cached_columns)

    events = []
    worker = _ProjectColumnsWorker("/fake/project", generation=17)
    worker.tier_ready.connect(
        lambda generation, columns: events.append(("ready", generation, columns))
    )
    worker.tier_failed.connect(
        lambda generation, tier, message: events.append(
            ("failed", generation, tier, message)
        )
    )

    worker.run()

    assert events == [
        ("failed", 17, "live", "RuntimeError: live failed"),
        ("ready", 17, cached_columns),
    ]


def test_project_columns_worker_reports_cached_failure_after_live_result(
    app, monkeypatch,
):
    from visualizers.project_visualizer import _ProjectColumnsWorker

    live_columns = [
        ProjectColumn(
            id="movies", title="Movies", count=12,
            datavis={"kind": "empty"}, state="ready",
        ),
    ]

    def fail_cached(_path):
        raise ValueError("cached failed")

    monkeypatch.setattr(corpus_stats_mod, "get_live_project_columns", lambda _path: live_columns)
    monkeypatch.setattr(corpus_stats_mod, "get_cached_project_columns", fail_cached)

    events = []
    worker = _ProjectColumnsWorker("/fake/project", generation=23)
    worker.tier_ready.connect(
        lambda generation, columns: events.append(("ready", generation, columns))
    )
    worker.tier_failed.connect(
        lambda generation, tier, message: events.append(
            ("failed", generation, tier, message)
        )
    )

    worker.run()

    assert events == [
        ("ready", 23, live_columns),
        ("failed", 23, "cached", "ValueError: cached failed"),
    ]


def test_project_visualizer_live_failure_settles_tier_and_preserves_cached_results(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        generation = window._project_load_generation
        cached_columns = [
            ProjectColumn(
                id="shots", title="Shots", count=4821,
                datavis={"kind": "empty"}, state="ready",
            ),
            ProjectColumn(
                id="vocabulary", title="Vocabulary", count=1832,
                datavis={"kind": "empty"}, state="ready",
            ),
        ]
        window._on_columns_tier_ready(generation, cached_columns)
        window._on_columns_tier_failed(generation, "live", "RuntimeError: live failed")

        widgets = window._project_column_widgets
        for col_id in (
            "movies", "gameplay", "silhouettes", "engravings",
        ):
            assert widgets[col_id].column.count is None
            assert widgets[col_id].column.state == "unavailable"
            assert widgets[col_id].column.reason == "live_tier_error"
        assert widgets["shots"].column.count == 4821
        assert widgets["shots"].column.state == "ready"
        assert widgets["vocabulary"].column.count == 1832
        assert window._project_load_state == "loaded"
        assert not window._column_loading_timer.isActive()
        assert all(
            widget.column.state != "loading"
            for widget in widgets.values()
        )
    finally:
        window.close()


def test_project_visualizer_cached_failure_settles_tier_and_preserves_live_results(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        generation = window._project_load_generation
        live_columns = [
            ProjectColumn(
                id="movies", title="Movies", count=0,
                datavis={"kind": "empty"}, state="ready",
            ),
            ProjectColumn(
                id="gameplay", title="Gameplay", count=3,
                datavis={"kind": "empty"}, state="ready",
            ),
            ProjectColumn(
                id="silhouettes", title="Silhouettes", count=7,
                datavis={"kind": "silhouette_fields", "fields": [
                    {"field": "animals", "count": 7},
                ]}, state="ready",
            ),
            ProjectColumn(
                id="engravings", title="Engravings", count=0,
                datavis={"kind": "engraving_fields", "fields": []}, state="ready",
            ),
        ]
        window._on_columns_tier_ready(generation, live_columns)
        window._on_columns_tier_failed(generation, "cached", "ValueError: cached failed")

        widgets = window._project_column_widgets
        assert widgets["movies"].column.count == 0
        assert widgets["movies"].column.state == "ready"
        assert widgets["movies"]._count_label.text() == "0"
        assert widgets["gameplay"].column.count == 3
        assert widgets["silhouettes"].column.count == 7
        assert widgets["engravings"].column.count == 0
        for col_id in ("shots", "vocabulary"):
            assert widgets[col_id].column.count is None
            assert widgets[col_id].column.state == "unavailable"
            assert widgets[col_id].column.reason == "cached_tier_error"
        assert window._project_load_state == "loaded"
        assert not window._column_loading_timer.isActive()
        assert all(
            widget.column.state != "loading"
            for widget in widgets.values()
        )
    finally:
        window.close()


def test_project_visualizer_ignores_stale_generation_tier_failure(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        current_generation = window._project_load_generation
        current_worker = window._project_columns_worker
        window._on_columns_tier_failed(
            current_generation - 1, "live", "RuntimeError: old failure",
        )

        assert window._project_load_generation == current_generation
        assert window._project_columns_worker is current_worker
        assert window._project_load_state == "loading"
        assert window._column_loading_timer.isActive()
        assert all(
            widget.column.state == "loading"
            for widget in window._project_column_widgets.values()
        )
    finally:
        window.close()


def test_project_visualizer_rejects_older_generation_without_affecting_current_load(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        current_generation = window._project_load_generation
        current_worker = window._project_columns_worker
        window._on_columns_tier_ready(current_generation - 1, [
            ProjectColumn(
                id="movies", title="Movies", count=12,
                datavis={"kind": "empty"}, state="ready",
            ),
        ])

        assert window._project_load_generation == current_generation
        assert window._project_columns_worker is current_worker
        assert window._project_load_state == "loading"
        assert window._column_loading_timer.isActive()
        assert all(
            widget.column.state == "loading"
            for widget in window._project_column_widgets.values()
        )

        window._on_columns_tier_ready(current_generation, [
            ProjectColumn(
                id="movies", title="Movies", count=99,
                datavis={"kind": "empty"}, state="ready",
            ),
        ])
        assert window._project_column_widgets["movies"]._count_label.text() == "99"
    finally:
        window.close()


def test_project_visualizer_old_live_tier_cannot_overwrite_new_generation(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        old_generation = window._project_load_generation
        window._start_project_columns_load(force=True)
        current_generation = window._project_load_generation
        assert current_generation == old_generation + 1

        window._on_columns_tier_ready(current_generation, [
            ProjectColumn(
                id="movies", title="Movies", count=99,
                datavis={"kind": "empty"}, state="ready",
            ),
        ])
        window._on_columns_tier_ready(old_generation, [
            ProjectColumn(
                id="movies", title="Movies", count=12,
                datavis={"kind": "empty"}, state="ready",
            ),
        ])

        widget = window._project_column_widgets["movies"]
        assert widget.column.count == 99
        assert widget._count_label.text() == "99"
        assert window._project_load_generation == current_generation
        assert window._project_load_state == "loading"
        assert window._column_loading_timer.isActive()
    finally:
        window.close()


def test_project_visualizer_old_cached_tier_cannot_overwrite_new_generation(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        old_generation = window._project_load_generation
        window._start_project_columns_load(force=True)
        current_generation = window._project_load_generation
        assert current_generation == old_generation + 1

        window._on_columns_tier_ready(current_generation, [
            ProjectColumn(
                id="vocabulary", title="Vocabulary", count=1832,
                datavis={"kind": "empty"}, state="ready",
            ),
        ])
        window._on_columns_tier_ready(old_generation, [
            ProjectColumn(
                id="vocabulary", title="Vocabulary", count=7,
                datavis={"kind": "empty"}, state="ready",
            ),
        ])

        widget = window._project_column_widgets["vocabulary"]
        assert widget.column.count == 1832
        assert widget._count_label.text() == "1.8k"
        assert window._project_load_generation == current_generation
        assert window._project_load_state == "loading"
        assert window._column_loading_timer.isActive()
    finally:
        window.close()


def test_project_visualizer_current_generation_tier_updates_only_matching_columns(
    app, fake_prefs, monkeypatch,
):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        live_columns = [
            ProjectColumn(id="movies", title="Movies", count=12, datavis={"kind": "empty"}, state="ready"),
            ProjectColumn(id="gameplay", title="Gameplay", count=3, datavis={"kind": "empty"}, state="ready"),
            ProjectColumn(id="silhouettes", title="Silhouettes", count=91, datavis={"kind": "empty"}, state="ready"),
            ProjectColumn(id="engravings", title="Engravings", count=28, datavis={"kind": "empty"}, state="ready"),
        ]
        window._on_columns_tier_ready(window._project_load_generation, live_columns)

        widgets = window._project_column_widgets
        assert widgets["movies"].column.state == "ready"
        assert widgets["movies"]._count_label.text() == "12"
        # The cached tier hasn't arrived yet — those columns stay loading.
        assert widgets["vocabulary"].column.state == "loading"
        assert widgets["vocabulary"]._loading_bar._active is True
        assert window._column_loading_timer.isActive()

        cached_columns = [
            ProjectColumn(id="shots", title="Shots", count=4821, datavis={"kind": "empty"}, state="ready"),
            ProjectColumn(id="vocabulary", title="Vocabulary", count=1832, datavis={"kind": "empty"}, state="ready"),
        ]
        window._on_columns_tier_ready(window._project_load_generation, cached_columns)

        # All six columns resolved — the shared loading timer stops.
        assert not window._column_loading_timer.isActive()
    finally:
        window.close()


def test_project_visualizer_unavailable_column_shows_compact_index_status(app, fake_prefs, monkeypatch):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        window._on_columns_tier_ready(window._project_load_generation, [
            ProjectColumn(id="vocabulary", title="Vocabulary", count=None, datavis={"kind": "empty"}, state="unavailable", reason="corpus_stats_missing"),
        ])
        widget = window._project_column_widgets["vocabulary"]
        assert widget._count_label.text() == "→ INDEX"
        assert "\n" not in widget._count_label.text()
        assert "—" not in widget._count_label.text()
        assert "INDEX REQUIRED" not in widget._count_label.text()
        assert widget._loading_bar._active is False

        widget.set_column(ProjectColumn(
            id="vocabulary", title="Vocabulary", count=None,
            datavis={"kind": "empty"}, state="stale", reason="vocabulary_index_stale",
        ))
        assert widget._count_label.text() == "INDEX STALE"
        assert "\n" not in widget._count_label.text()
        assert "\u2014" not in widget._count_label.text()
        assert widget.column.state == "stale"
    finally:
        window.close()


def test_project_visualizer_worker_populates_columns_end_to_end(app, fake_prefs, monkeypatch):
    """Real QThread run, not mocked — proves the signal wiring actually works."""
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr(
        corpus_stats_mod, "get_live_project_columns",
        lambda p: [ProjectColumn(id="movies", title="Movies", count=314, datavis={"kind": "empty"}, state="ready"),
                   ProjectColumn(id="gameplay", title="Gameplay", count=2, datavis={"kind": "empty"}, state="ready"),
                   ProjectColumn(id="silhouettes", title="Silhouettes", count=883723, datavis={"kind": "empty"}, state="ready"),
                   ProjectColumn(id="engravings", title="Engravings", count=4123, datavis={"kind": "empty"}, state="ready")],
    )
    monkeypatch.setattr(
        corpus_stats_mod, "get_cached_project_columns",
        lambda p: [ProjectColumn(id="shots", title="Shots", count=285500, datavis={"kind": "empty"}, state="ready"),
               ProjectColumn(id="vocabulary", title="Vocabulary", count=8418, datavis={"kind": "empty"}, state="ready")],
    )

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        assert window._project_columns_worker.wait(5000), "worker did not finish in time"
        app.processEvents()

        widgets = window._project_column_widgets
        assert widgets["movies"]._count_label.text() == "314"
        assert widgets["shots"]._count_label.text() == "285.5k"
        assert all(w.column.state == "ready" for w in widgets.values())
        assert not window._column_loading_timer.isActive()
    finally:
        window.close()


def test_project_visualizer_browse_restarts_column_loading(app, fake_prefs, monkeypatch, tmp_path):
    monkeypatch.setattr(
        corpus_stats_mod, "get_live_project_columns",
        lambda p: [ProjectColumn(id="movies", title="Movies", count=1, datavis={"kind": "empty"}, state="ready"),
                   ProjectColumn(id="gameplay", title="Gameplay", count=1, datavis={"kind": "empty"}, state="ready"),
                   ProjectColumn(id="shots", title="Shots", count=1, datavis={"kind": "empty"}, state="ready")],
    )
    monkeypatch.setattr(corpus_stats_mod, "get_cached_project_columns", lambda p: [])

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        assert window._project_columns_worker.wait(5000)
        app.processEvents()

        monkeypatch.setattr(
            corpus_stats_mod, "get_live_project_columns",
            lambda p: [ProjectColumn(id="movies", title="Movies", count=99, datavis={"kind": "empty"}, state="ready"),
                       ProjectColumn(id="gameplay", title="Gameplay", count=1, datavis={"kind": "empty"}, state="ready"),
                       ProjectColumn(id="shots", title="Shots", count=1, datavis={"kind": "empty"}, state="ready")],
        )
        monkeypatch.setattr(
            "visualizers.project_visualizer.QFileDialog.getExistingDirectory",
            lambda *a, **k: str(tmp_path),
        )
        window._on_browse()

        assert window._project_column_widgets["movies"].column.state == "loading"
        assert window._project_load_generation == 2
        assert window._project_load_state == "loading"
        assert window._project_columns_worker.wait(5000)
        app.processEvents()

        assert window._project_column_widgets["movies"]._count_label.text() == "99"
    finally:
        window.close()


@pytest.mark.parametrize("count, expected", [
    (None, "\u2014"),
    (0, "0"),
    (999, "999"),
    (1000, "1k"),
    (1800, "1.8k"),
    (1445, "1.4k"),
    (2433, "2.4k"),
    (3972, "4k"),
    (48000, "48k"),
    (48213, "48.2k"),
    (285473, "285.5k"),
    (898557, "898.6k"),
    (1_000_000, "1M"),
    (1_200_000, "1.2M"),
    (2_500_000, "2.5M"),
])
def test_format_column_count(count, expected):
    from visualizers.project_visualizer import _format_column_count

    assert _format_column_count(count) == expected
