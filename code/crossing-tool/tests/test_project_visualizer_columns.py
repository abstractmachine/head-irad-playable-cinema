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
import threading

import pytest
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication, QLabel, QScrollArea

import services.corpus_stats as corpus_stats_mod
import services.illustration_index as illustration_index_mod
import services.silhouette_catalog as silhouette_catalog_mod
from styles import theme
from services.corpus_stats import (
    PROJECT_COLUMN_IDS_AND_TITLES,
    ProjectColumn,
    corpus_stats_cache_path,
    get_cached_project_columns,
    get_illustration_stats,
    get_live_project_columns,
    get_project_columns,
    get_vocabulary_field_counts,
    load_cached_corpus_stats,
    refresh_corpus_stats_cache,
)

EXPECTED_COLUMN_IDS = (
    "movies", "gameplay", "shots", "vocabulary", "segments", "flipbooks",
    "illustrations",
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


# ---------------------------------------------------------------------------
# Data model — live tier (Movies/Gameplay/Shots)
# ---------------------------------------------------------------------------

def test_get_live_project_columns_computes_from_metadata_and_annotations(tmp_path, monkeypatch):
    _write_metadata(tmp_path, "movie", ["a.mp4", "b.mp4"])
    _write_metadata(tmp_path, "gameplay", ["c.mp4"])
    _write_annotations(tmp_path, "movie", "a", 3)
    _write_annotations(tmp_path, "movie", "b", 2)
    monkeypatch.setattr(
        corpus_stats_mod, "get_illustration_stats",
        lambda p: {"state": "ready", "count": 7, "labels": {"horse": 7}},
    )

    columns = get_live_project_columns(str(tmp_path))

    assert [c.id for c in columns] == ["movies", "gameplay", "shots", "illustrations"]
    by_id = {c.id: c for c in columns}
    assert by_id["movies"].count == 2
    assert by_id["gameplay"].count == 1
    assert by_id["shots"].count == 5
    assert by_id["illustrations"].count == 7
    assert all(c.state == "ready" for c in columns)
    assert all(c.datavis == {"kind": "empty"} for c in columns)


def test_get_live_project_columns_without_project_path_are_unavailable():
    columns = get_live_project_columns(None)

    assert all(c.count is None for c in columns)
    assert all(c.state == "unavailable" for c in columns)


def test_get_live_project_columns_empty_project_reports_zero_not_unavailable(tmp_path, monkeypatch):
    # An empty-but-real project directory has a known answer (zero) for
    # Movies/Gameplay/Shots, which is semantically different from "we don't
    # know" (state="unavailable"). Illustrations is different: with no
    # illustration index ever built, we genuinely don't know the count, so
    # it stays "unavailable" even for an otherwise-empty project.
    columns = get_live_project_columns(str(tmp_path))
    by_id = {c.id: c for c in columns}

    for col_id in ("movies", "gameplay", "shots"):
        assert by_id[col_id].count == 0
        assert by_id[col_id].state == "ready"

    assert by_id["illustrations"].count is None
    assert by_id["illustrations"].state == "unavailable"
    assert by_id["illustrations"].reason == "illustration_index_missing"


# ---------------------------------------------------------------------------
# Data model — cached tier (Vocabulary/Segments/Flipbooks/Illustrations)
# ---------------------------------------------------------------------------

def test_get_cached_project_columns_reads_stats_cache(monkeypatch):
    fake_stats = {
        "vocabulary_terms": 1832,
        "detected_scenes": 57,
        "flipbooks": 2,
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

    assert [c.id for c in columns] == ["vocabulary", "segments", "flipbooks"]
    counts = {c.id: c.count for c in columns}
    assert counts == {"vocabulary": 1832, "segments": 57, "flipbooks": 2}
    assert all(c.state == "ready" for c in columns)
    assert columns[0].datavis == {
        "kind": "vocabulary_fields",
        "fields": [{"field": "objects", "count": 1832}],
    }


def test_get_cached_project_columns_keeps_stale_vocabulary_distinct(monkeypatch):
    monkeypatch.setattr(
        corpus_stats_mod, "get_corpus_stats_state",
        lambda p: {
            "state": "ready",
            "stats": {"vocabulary_terms": 2, "detected_scenes": 7, "flipbooks": 3},
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

    assert columns[0].count is None
    assert columns[0].state == "stale"
    assert columns[0].reason == "vocabulary_index_stale"
    assert columns[0].datavis == {"kind": "empty"}
    assert [(column.id, column.count, column.state) for column in columns[1:]] == [
        ("segments", 7, "ready"),
        ("flipbooks", 3, "ready"),
    ]


def test_get_cached_project_columns_surfaces_vocabulary_count_mismatch(monkeypatch):
    monkeypatch.setattr(
        corpus_stats_mod, "get_corpus_stats_state",
        lambda p: {
            "state": "ready",
            "stats": {"vocabulary_terms": 3, "detected_scenes": 7, "flipbooks": 3},
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

    assert columns[0].count is None
    assert columns[0].state == "stale"
    assert columns[0].reason == "vocabulary_count_mismatch"
    assert columns[0].datavis == {"kind": "empty"}
    assert [(column.id, column.count) for column in columns[1:]] == [
        ("segments", 7),
        ("flipbooks", 3),
    ]


def test_vocabulary_field_counts_follow_index_order_and_match_primary_total(monkeypatch):
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
        {"field": "settings", "count": 2},
        {"field": "objects", "count": 3},
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
        lambda p: {"state": "ready", "stats": {"vocabulary_terms": 1, "detected_scenes": 2, "flipbooks": 3}},
    )

    columns = get_project_columns(None)  # live tier -> unavailable, cached tier -> ready

    assert [c.id for c in columns] == list(EXPECTED_COLUMN_IDS)
    assert [c.title for c in columns] == [
        "Movies", "Gameplay", "Shots", "Vocabulary", "Segments", "Flipbooks", "Illustrations",
    ]
    assert all(c.datavis == {"kind": "empty"} for c in columns)


def test_project_column_ids_and_titles_matches_get_project_columns_order():
    columns = get_project_columns(None)

    assert PROJECT_COLUMN_IDS_AND_TITLES == tuple((c.id, c.title) for c in columns)


# ---------------------------------------------------------------------------
# Persisted stats cache
# ---------------------------------------------------------------------------

def test_refresh_corpus_stats_cache_writes_and_load_reads_it_back(tmp_path, monkeypatch):
    fake_stats = {"movie_videos": 9, "vocabulary_terms": 5}
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

    assert result == {"state": "stale", "reason": "illustration_index_stale"}


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
        assert layout.count() == 7
        assert all(layout.stretch(i) == 1 for i in range(7))  # equal-width, responsive grid

        for col_id, title in PROJECT_COLUMN_IDS_AND_TITLES:
            widget = widgets[col_id]
            assert widget.column.state == "loading"
            assert widget._header_label.text() == title.upper()
            assert widget._count_label.text() == "loading…"
            assert widget._loading_bar._active is True
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
            for col_id, title in PROJECT_COLUMN_IDS_AND_TITLES[:3]
        ] + [
            ProjectColumn(
                id="illustrations", title="Illustrations", count=1,
                datavis={"kind": "empty"}, state="ready",
            )
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
            for col_id, title in PROJECT_COLUMN_IDS_AND_TITLES[3:6]
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
        window._on_columns_tier_ready([
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


def test_project_visualizer_tier_ready_updates_only_matching_columns(app, fake_prefs, monkeypatch):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        live_columns = [
            ProjectColumn(id="movies", title="Movies", count=12, datavis={"kind": "empty"}, state="ready"),
            ProjectColumn(id="gameplay", title="Gameplay", count=3, datavis={"kind": "empty"}, state="ready"),
            ProjectColumn(id="shots", title="Shots", count=4821, datavis={"kind": "empty"}, state="ready"),
        ]
        window._on_columns_tier_ready(live_columns)

        widgets = window._project_column_widgets
        assert widgets["movies"].column.state == "ready"
        assert widgets["movies"]._count_label.text() == "12"
        # The cached tier hasn't arrived yet — those columns stay loading.
        assert widgets["vocabulary"].column.state == "loading"
        assert widgets["vocabulary"]._loading_bar._active is True
        assert window._column_loading_timer.isActive()

        cached_columns = [
            ProjectColumn(id="vocabulary", title="Vocabulary", count=1832, datavis={"kind": "empty"}, state="ready"),
            ProjectColumn(id="segments", title="Segments", count=57, datavis={"kind": "empty"}, state="ready"),
            ProjectColumn(id="flipbooks", title="Flipbooks", count=2, datavis={"kind": "empty"}, state="ready"),
            ProjectColumn(id="illustrations", title="Illustrations", count=48213, datavis={"kind": "empty"}, state="ready"),
        ]
        window._on_columns_tier_ready(cached_columns)

        assert widgets["illustrations"]._count_label.text() == "48.2k"
        assert widgets["illustrations"].column.state == "ready"
        assert widgets["illustrations"]._loading_bar._active is False
        # All seven columns resolved — the shared loading timer stops.
        assert not window._column_loading_timer.isActive()
    finally:
        window.close()


def test_project_visualizer_unavailable_column_shows_compact_index_status(app, fake_prefs, monkeypatch):
    fake_prefs["path"] = "/fake/project"
    monkeypatch.setattr("visualizers.project_visualizer._ProjectColumnsWorker.start", lambda self: None)

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        window._on_columns_tier_ready([
            ProjectColumn(id="vocabulary", title="Vocabulary", count=None, datavis={"kind": "empty"}, state="unavailable", reason="corpus_stats_missing"),
            ProjectColumn(id="segments", title="Segments", count=None, datavis={"kind": "empty"}, state="unavailable", reason="corpus_stats_missing"),
            ProjectColumn(id="flipbooks", title="Flipbooks", count=None, datavis={"kind": "empty"}, state="unavailable", reason="corpus_stats_missing"),
        ])
        widget = window._project_column_widgets["vocabulary"]
        assert widget._count_label.text() == "→ INDEX"
        assert "\n" not in widget._count_label.text()
        assert "—" not in widget._count_label.text()
        assert "INDEX REQUIRED" not in widget._count_label.text()
        assert widget._loading_bar._active is False

        illustrations = window._project_column_widgets["illustrations"]
        illustrations.set_column(ProjectColumn(
            id="illustrations", title="Illustrations", count=None,
            datavis={"kind": "empty"}, state="unavailable",
            reason="illustration_index_missing",
        ))
        assert illustrations._count_label.text() == "NEEDS INDEX"
        assert "\n" not in illustrations._count_label.text()

        widget.set_column(ProjectColumn(
            id="vocabulary", title="Vocabulary", count=None,
            datavis={"kind": "empty"}, state="stale", reason="corpus_stats_stale",
        ))
        assert widget._count_label.text() == "\u2014\nSTATS STALE"
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
                   ProjectColumn(id="shots", title="Shots", count=285500, datavis={"kind": "empty"}, state="ready")],
    )
    monkeypatch.setattr(
        corpus_stats_mod, "get_cached_project_columns",
        lambda p: [ProjectColumn(id="vocabulary", title="Vocabulary", count=8418, datavis={"kind": "empty"}, state="ready"),
                   ProjectColumn(id="segments", title="Segments", count=28200, datavis={"kind": "empty"}, state="ready"),
                   ProjectColumn(id="flipbooks", title="Flipbooks", count=314, datavis={"kind": "empty"}, state="ready"),
                   ProjectColumn(id="illustrations", title="Illustrations", count=883723, datavis={"kind": "empty"}, state="ready")],
    )

    from visualizers.project_visualizer import ProjectVisualizer

    window = ProjectVisualizer()
    try:
        assert window._project_columns_worker.wait(5000), "worker did not finish in time"
        app.processEvents()

        widgets = window._project_column_widgets
        assert widgets["movies"]._count_label.text() == "314"
        assert widgets["shots"]._count_label.text() == "285.5k"
        assert widgets["illustrations"]._count_label.text() == "883.7k"
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
