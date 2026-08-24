import json
import subprocess

import pytest
from PyQt5.QtWidgets import QApplication

from data.media_id import build_shot_id
from data.metadata import save_json_metadata
from data.shotlist import write_shotlist
import services.search as search_mod
import services.vocabulary_index as vocabulary_index_mod
import visualizers.mosaic_visualizer as mosaic_mod
from data.index import save_atomic_fields, save_vocabulary_fields
from services.derived_vocabulary import build_derived_vocabulary
from services.vocabulary_index import build_vocabulary_index
from visualizers.mosaic_visualizer import (
    MosaicVisualizer,
    SearchWorker,
    VOCABULARY_RENDER_THRESHOLD,
    VocabularyIndexWorker,
    VocabularyWorker,
)


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


def _write_annotations(project_path, values):
    annotation_dir = project_path / "data" / "annotations" / "shots" / "movie"
    annotation_dir.mkdir(parents=True, exist_ok=True)
    annotation_path = annotation_dir / "Film.annotations.json"
    annotation_path.write_text(
        json.dumps([
            {"shot": {"annotation": {"animals": values}}},
        ]),
        encoding="utf-8",
    )
    return annotation_path


def test_index_only_vocabulary_reports_missing_without_raw_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "data.metadata.get_metadata",
        lambda *args, **kwargs: pytest.fail("raw annotation fallback was used"),
    )

    result = search_mod.vocabulary_from_index("animals", str(tmp_path), "movie")

    assert result == {"status": "missing"}


def test_index_only_vocabulary_reports_stale_without_raw_scan(tmp_path, monkeypatch):
    save_vocabulary_fields(str(tmp_path), ["animals"])
    annotation_path = _write_annotations(tmp_path, ["horse"])
    build_vocabulary_index(str(tmp_path), "movie")
    annotation_path.write_text(
        json.dumps([{"shot": {"annotation": {"animals": ["horse", "wolf"]}}}]),
        encoding="utf-8",
    )
    vocabulary_index_mod._source_hash_cache.clear()
    monkeypatch.setattr(
        "data.metadata.get_metadata",
        lambda *args, **kwargs: pytest.fail("raw annotation fallback was used"),
    )

    result = search_mod.vocabulary_from_index("animals", str(tmp_path), "movie")

    assert result == {"status": "stale"}


def test_index_only_all_field_merges_indexed_vocabulary(tmp_path, monkeypatch):
    save_vocabulary_fields(str(tmp_path), ["animals", "objects"])
    annotation_dir = tmp_path / "data" / "annotations" / "shots" / "movie"
    annotation_dir.mkdir(parents=True, exist_ok=True)
    (annotation_dir / "Film.annotations.json").write_text(
        json.dumps([
            {
                "shot": {
                    "annotation": {
                        "animals": ["horse"],
                        "objects": ["horse", "saddle"],
                    }
                }
            }
        ]),
        encoding="utf-8",
    )
    build_vocabulary_index(str(tmp_path), "movie")
    monkeypatch.setattr(
        "data.metadata.get_metadata",
        lambda *args, **kwargs: pytest.fail("raw annotation fallback was used"),
    )

    result = search_mod.vocabulary_from_index(
        "--all", str(tmp_path), "movie", sort="count", family="canonical"
    )

    assert result == {
        "status": "ready",
        "items": [
            {"value": "horse", "count": 2},
            {"value": "saddle", "count": 1},
        ],
    }


def test_index_only_vocabulary_includes_configured_atomic_fields(tmp_path):
    save_vocabulary_fields(str(tmp_path), ["animals"])
    save_atomic_fields(str(tmp_path), ["action"])
    annotation_dir = tmp_path / "data" / "annotations" / "shots" / "movie"
    annotation_dir.mkdir(parents=True, exist_ok=True)
    (annotation_dir / "Film.annotations.json").write_text(
        json.dumps([
            {"shot": {"annotation": {"action": ["勇禁"]}}},
        ]),
        encoding="utf-8",
    )
    build_vocabulary_index(str(tmp_path), "movie")

    result = search_mod.vocabulary_from_index("action", str(tmp_path), "movie")

    assert result == {"status": "ready", "items": [{"value": "勇禁", "count": 1}]}


def test_index_only_all_field_merges_canonical_and_derived_vocabulary(tmp_path):
    save_vocabulary_fields(str(tmp_path), ["animals"])
    annotation_dir = tmp_path / "data" / "annotations" / "shots" / "movie"
    annotation_dir.mkdir(parents=True, exist_ok=True)
    (annotation_dir / "Film.annotations.json").write_text(
        json.dumps([
            {"shot": {"annotation": {
                "animals": ["horse"], "description": "Two wagons pass.",
            }}},
            {"shot": {"annotation": {
                "description": "A wagon waits.", "text": "Zealand poster.",
            }}},
            {"shot": {"annotation": {"text": "Zealand sign."}}},
        ]),
        encoding="utf-8",
    )
    build_vocabulary_index(str(tmp_path), "movie")
    build_derived_vocabulary(str(tmp_path), "movie")

    result = search_mod.vocabulary_from_index("--all", str(tmp_path), "movie", sort="count")

    assert result == {
        "status": "ready",
        "items": [
            {"value": "wagon", "count": 2},
            {"value": "zealand", "count": 2},
            {"value": "horse", "count": 1},
        ],
    }


def test_index_only_count_alphabetical_sorts_equal_counts_by_value(tmp_path):
    save_vocabulary_fields(str(tmp_path), ["animals"])
    annotation_dir = tmp_path / "data" / "annotations" / "shots" / "movie"
    annotation_dir.mkdir(parents=True, exist_ok=True)
    (annotation_dir / "Film.annotations.json").write_text(
        json.dumps([
            {"shot": {"annotation": {
                "animals": ["zebra"], "description": "A wagon waits.",
            }}},
            {"shot": {"annotation": {
                "animals": ["zebra"], "description": "The wagon passes.",
            }}},
        ]),
        encoding="utf-8",
    )
    build_vocabulary_index(str(tmp_path), "movie")
    build_derived_vocabulary(str(tmp_path), "movie")

    result = search_mod.vocabulary_from_index(
        "--all", str(tmp_path), "movie", sort="count_alphabetical"
    )

    assert result["items"] == [
        {"value": "wagon", "count": 2},
        {"value": "zebra", "count": 2},
    ]


def test_vocabulary_worker_rejects_scoped_live_reconstruction(monkeypatch):
    monkeypatch.setattr(
        search_mod,
        "vocabulary_from_index",
        lambda **kwargs: {"status": "ready", "items": [{"value": "horse", "count": 2}]},
    )
    results = []
    worker = VocabularyWorker("animals", "Film.mp4", "/project", "movie")
    worker.result_ready.connect(results.append)

    worker.run()

    assert results == [{"status": "scope_unsupported"}]


def test_vocabulary_worker_prepares_large_result_navigation(monkeypatch):
    items = [
        {"value": f"apple {index}", "count": index}
        for index in range(VOCABULARY_RENDER_THRESHOLD)
    ] + [{"value": "zebra", "count": 1}]
    monkeypatch.setattr(
        search_mod,
        "vocabulary_from_index",
        lambda **kwargs: {"status": "ready", "items": items},
    )
    results = []
    worker = VocabularyWorker("objects", None, "/project", "movie")
    worker.result_ready.connect(results.append)

    worker.run()

    result = results[0]
    assert result["is_large"] is True
    assert result["initials"] == ["a", "z"]
    assert result["selected_prefix"] == "--all"
    assert len(result["items"]) == VOCABULARY_RENDER_THRESHOLD + 1


def test_vocabulary_worker_prepares_requested_prefix(monkeypatch):
    items = [
        {"value": "apple", "count": 2},
        {"value": "zebra", "count": 1},
    ]
    monkeypatch.setattr(
        search_mod,
        "vocabulary_from_index",
        lambda **kwargs: {"status": "ready", "items": items},
    )
    results = []
    worker = VocabularyWorker("objects", None, "/project", "movie", prefix="z")
    worker.result_ready.connect(results.append)

    worker.run()

    assert results[0]["items"] == [{"value": "zebra", "count": 1}]
    assert results[0]["selected_prefix"] == "z"


@pytest.mark.parametrize(
    "sort", ["count", "alphabetical", "count_alphabetical"],
)
def test_vocabulary_worker_passes_requested_sort(monkeypatch, sort):
    calls = []
    monkeypatch.setattr(
        search_mod,
        "vocabulary_from_index",
        lambda **kwargs: calls.append(kwargs) or {"status": "ready", "items": []},
    )
    results = []
    worker = VocabularyWorker("objects", None, "/project", sort=sort)
    worker.result_ready.connect(results.append)

    worker.run()

    assert calls[0]["sort"] == sort


def test_vocabulary_worker_uses_selected_field_for_family_routing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        search_mod,
        "vocabulary_from_index",
        lambda **kwargs: calls.append(kwargs) or {"status": "ready", "items": []},
    )
    worker = VocabularyWorker("description", None, "/project")

    worker.run()

    assert calls == [{
        "field": "description", "show_count": True, "project_path": "/project",
        "media_type": "movie", "sort": "count",
    }]


def test_rebuild_worker_invokes_existing_cli_command(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "Saved: vocabulary_movie.json\n", "")

    monkeypatch.setattr(mosaic_mod.subprocess, "run", fake_run)
    outputs = []
    worker = VocabularyIndexWorker("movie")
    worker.finished_signal.connect(outputs.append)

    worker.run()

    command, kwargs = calls[0]
    assert command == [
        mosaic_mod.sys.executable,
        str(mosaic_mod._CLI_PATH),
        "index",
        "vocabulary",
        "--media",
        "movie",
        "--family",
        "canonical",
        "--force",
    ]
    assert kwargs == {"capture_output": True, "text": True, "check": False}
    assert outputs == ["Saved: vocabulary_movie.json"]


def test_rebuild_worker_can_target_derived_vocabulary(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mosaic_mod.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )
    worker = VocabularyIndexWorker("gameplay", "derived")

    worker.run()

    assert calls[0][-5:] == ["--media", "gameplay", "--family", "derived", "--force"]


def test_mosaic_uses_annotation_field_for_derived_vocabulary(tmp_path, app, fake_prefs, monkeypatch):
    window = MosaicVisualizer(str(tmp_path))
    requested = []
    monkeypatch.setattr(window, "_start_vocabulary_load", lambda prefix=None: requested.append(prefix))

    window.field_combo.setCurrentIndex(window.field_combo.findData("description"))

    assert not hasattr(window, "vocab_family_combo")
    assert window.field_combo.currentData() == "description"
    assert requested == [None]
    window.close()


def test_mosaic_inspector_shows_stale_and_rebuild_busy_states(
    tmp_path, monkeypatch, app, fake_prefs
):
    monkeypatch.setattr(VocabularyIndexWorker, "start", lambda self: None)
    window = MosaicVisualizer(str(tmp_path))

    window._on_vocab_result({"status": "stale"})
    assert window.vocab_table._rows[0].text() == (
        "Vocabulary index is stale. Rebuild it to continue."
    )
    assert window.vocab_rebuild_btn.isEnabled()

    window._on_vocab_rebuild()
    assert not window.vocab_rebuild_btn.isEnabled()
    assert window.vocab_rebuild_btn.text() == "Rebuilding..."
    assert window.vocab_table._rows[0].text() == "Rebuilding vocabulary index..."

    window._on_vocab_rebuild_error("command failed")
    assert window.vocab_rebuild_btn.isEnabled()
    assert window.vocab_rebuild_btn.text() == "Rebuild Vocabulary"
    assert window.vocab_table._rows[0].text() == "Rebuild failed: command failed"

    window._on_vocab_result({"status": "ready", "items": []})
    assert not window.vocab_rebuild_btn.isEnabled()
    window.close()


def test_large_vocabulary_uses_navigation_and_incremental_population(
    tmp_path, app, fake_prefs
):
    window = MosaicVisualizer(str(tmp_path))
    items = [
        {"value": f"item {index}", "count": index}
        for index in range(VOCABULARY_RENDER_THRESHOLD + 1)
    ]
    window._vocab_loading_bar.start()
    window._vocab_loading_timer.start()

    window._on_vocab_result({
        "status": "ready",
        "items": items,
        "total": len(items),
        "initials": ["i"],
        "selected_prefix": "--all",
        "is_large": True,
    })

    assert window.vocab_nav_combo.isVisibleTo(window)
    assert window.vocab_nav_combo.itemData(0) == "--all"
    assert window.vocab_nav_combo.itemData(1) == "i"
    assert window.vocab_table._rows == []
    assert window.vocab_table.isHidden()
    assert not window.vocab_table.updatesEnabled()
    assert not window.vocab_table._layout.isEnabled()
    assert window._vocab_loading_bar._active is True

    while window.vocab_table._population_timer.isActive():
        app.processEvents()

    assert len(window.vocab_table._rows) == len(items)
    assert not window.vocab_table.isHidden()
    assert window.vocab_table.updatesEnabled()
    assert window.vocab_table._layout.isEnabled()
    assert window._vocab_loading_bar._active is False
    window.close()


def test_small_vocabulary_keeps_all_and_letter_navigation(tmp_path, app, fake_prefs):
    window = MosaicVisualizer(str(tmp_path))

    window._on_vocab_result({
        "status": "ready",
        "items": [
            {"value": "apple", "count": 2},
            {"value": "zebra", "count": 1},
        ],
        "total": 2,
        "initials": ["a", "z"],
        "selected_prefix": "--all",
        "is_large": False,
    })

    assert window.vocab_nav_combo.isVisibleTo(window)
    assert [
        window.vocab_nav_combo.itemData(index)
        for index in range(window.vocab_nav_combo.count())
    ] == ["--all", "a", "z"]
    assert window.vocab_nav_combo.currentData() == "--all"
    assert len(window.vocab_table._rows) == 2
    window.close()


def test_all_annotation_field_starts_vocabulary_worker(tmp_path, monkeypatch, app, fake_prefs):
    window = MosaicVisualizer(str(tmp_path))
    requested = []
    monkeypatch.setattr(
        window,
        "_start_vocabulary_load",
        lambda prefix=None: requested.append(prefix),
    )
    window.field_combo.setCurrentText("--all")

    window._on_field_changed()

    assert requested == [None]
    window.close()


def test_annotation_field_change_resets_vocabulary_navigation_to_all(
    tmp_path, monkeypatch, app, fake_prefs
):
    window = MosaicVisualizer(str(tmp_path))
    window.vocab_nav_combo.blockSignals(True)
    window.vocab_nav_combo.addItem("a", "a")
    window.vocab_nav_combo.setCurrentIndex(0)
    window.vocab_nav_combo.blockSignals(False)
    requested = []
    monkeypatch.setattr(
        window,
        "_start_vocabulary_load",
        lambda prefix=None: requested.append(prefix),
    )

    window.field_combo.setCurrentIndex(window.field_combo.findData("objects"))

    assert window.vocab_nav_combo.count() == 0
    assert requested == [None]

    window._on_vocab_result({
        "status": "ready",
        "items": [{"value": "saddle", "count": 1}],
        "total": 1,
        "initials": ["s"],
        "selected_prefix": "--all",
        "is_large": False,
    })
    assert window.vocab_nav_combo.currentText() == "<A-Z>"
    assert window.vocab_nav_combo.currentData() == "--all"
    window.close()


def test_first_show_loads_all_field_vocabulary_once(
    tmp_path, monkeypatch, app, fake_prefs
):
    started_fields = []
    monkeypatch.setattr(
        VocabularyWorker,
        "start",
        lambda self: started_fields.append(self.field),
    )
    window = MosaicVisualizer(str(tmp_path))

    window.show()
    app.processEvents()
    window.hide()
    window.show()
    app.processEvents()

    assert window.field_combo.currentText() == "<All Fields>"
    assert window.field_combo.currentData() == "--all"
    assert started_fields == ["--all"]
    window.close()


def test_vocabulary_reload_clears_and_shows_loading_before_worker_starts(
    tmp_path, monkeypatch, app, fake_prefs
):
    window = MosaicVisualizer(str(tmp_path))
    window.vocab_table.set_items([{"value": "horse", "count": 1}])
    started = []
    monkeypatch.setattr(VocabularyWorker, "start", lambda self: started.append(self))

    window._start_vocabulary_load()

    assert window.vocab_table._rows == []
    assert window._vocab_loading_bar._active is True
    assert started == []

    app.processEvents()

    assert len(started) == 1
    window.close()


def test_navigation_change_starts_worker_request(tmp_path, monkeypatch, app, fake_prefs):
    window = MosaicVisualizer(str(tmp_path))
    requested = []
    monkeypatch.setattr(window, "_start_vocabulary_load", requested.append)
    window.vocab_nav_combo.blockSignals(True)
    window.vocab_nav_combo.addItem("--all", "--all")
    window.vocab_nav_combo.addItem("a", "a")
    window.vocab_nav_combo.blockSignals(False)

    window.vocab_nav_combo.setCurrentIndex(1)

    assert requested == ["a"]
    window.close()


def test_mosaic_search_clear_button_tracks_browser_results(tmp_path, app, fake_prefs):
    window = MosaicVisualizer(str(tmp_path))
    button_grid = window.clear_btn.parentWidget().layout().itemAt(1).layout()

    assert window.clear_btn.text() == "Clear"
    assert not window.clear_btn.isEnabled()
    assert button_grid.itemAtPosition(0, 0).widget() is window.search_btn
    assert button_grid.itemAtPosition(0, 1).widget() is window.best_btn
    assert button_grid.itemAtPosition(1, 0).widget() is window.shotlist_btn
    assert button_grid.itemAtPosition(1, 1).widget() is window.clear_btn
    assert button_grid.itemAtPosition(2, 0).widget() is window.pdf_btn
    assert button_grid.itemAtPosition(2, 1).widget() is window.video_btn
    window._current_results = [{"shot_id": "1"}]
    window._update_result_controls()

    assert window.clear_btn.isEnabled()
    window._on_clear()

    assert window._current_results == []
    assert not window.clear_btn.isEnabled()
    assert window.search_status_label.text() == "Enter a query and press Search"
    window.close()


def test_mosaic_scope_offers_all_media_and_keeps_title_media_type(tmp_path, app, fake_prefs, monkeypatch):
    def fake_metadata(_project_path, media_type):
        return [{
            "title": media_type.title(), "year": "2026", "filename": f"{media_type}.mp4",
        }]

    monkeypatch.setattr("data.metadata.get_metadata", fake_metadata)
    window = MosaicVisualizer(str(tmp_path))
    window.media_type_combo.setCurrentIndex(0)

    assert [window.media_type_combo.itemData(index) for index in range(3)] == [
        "--all", None, None,
    ]
    assert window.media_type_combo.itemText(0) == "<All Media>"
    scopes = [window.movie_combo.itemData(index) for index in range(window.movie_combo.count())]
    assert ("movie", "movie.mp4") in scopes
    assert ("gameplay", "gameplay.mp4") in scopes
    window.movie_combo.setCurrentIndex(scopes.index(("gameplay", "gameplay.mp4")))
    assert window._current_scope() == ("gameplay", "gameplay.mp4")
    window.close()


def test_mosaic_restores_and_persists_vocabulary_sort(tmp_path, app, fake_prefs, monkeypatch):
    fake_prefs["mosaic_vocabulary_sort"] = "count_alphabetical"
    window = MosaicVisualizer(str(tmp_path))
    requested = []
    monkeypatch.setattr(window, "_start_vocabulary_load", requested.append)

    assert window.vocab_sort_combo.currentData() == "count_alphabetical"
    window.vocab_sort_combo.setCurrentIndex(window.vocab_sort_combo.findData("alphabetical"))

    assert fake_prefs["mosaic_vocabulary_sort"] == "alphabetical"
    assert requested == ["--all"]
    window.close()


def test_all_media_search_worker_queries_movie_and_gameplay(monkeypatch):
    calls = []
    monkeypatch.setattr(
        search_mod,
        "search_shots",
        lambda **kwargs: calls.append(kwargs["media_type"]) or {"results": [{
            "movie_id": kwargs["media_type"], "shot_id": "1",
        }]},
    )
    results = []
    worker = SearchWorker("query", None, None, None, False, "/project", media_type="--all")
    worker.tile_ready.connect(lambda result, _pixmap: results.append(result))

    worker.run()

    assert calls == ["movie", "gameplay"]
    assert [result["media_type"] for result in results] == ["movie", "gameplay"]


def test_search_shots_multiword_query_requires_all_terms(tmp_path):
    filename = "Film One.mp4"
    media_id = "file_film_one"
    save_json_metadata(tmp_path, "movie", [{
        "filename": filename,
        "title": "Film One",
        "media_id": media_id,
    }])
    write_shotlist(tmp_path, filename, "movie", [
        {
            "Scene": "1",
            "start_time": "00:00:00.000",
            "end_time": "00:00:10.000",
            "start_frame": 0,
            "end_frame": 240,
            "shot_id": build_shot_id(media_id, 0, 240),
        },
        {
            "Scene": "1",
            "start_time": "00:00:10.000",
            "end_time": "00:00:20.000",
            "start_frame": 241,
            "end_frame": 480,
            "shot_id": build_shot_id(media_id, 241, 480),
        },
    ])
    ann_dir = tmp_path / "data" / "annotations" / "shots" / "movie"
    ann_dir.mkdir(parents=True, exist_ok=True)
    (ann_dir / "Film One.annotations.json").write_text(
        json.dumps([
            {"shot": {"shot_id": build_shot_id(media_id, 0, 240), "annotation": {"wearing": ["cowboy hat", "shirt", "pants", "boots"]}}},
            {"shot": {"shot_id": build_shot_id(media_id, 241, 480), "annotation": {"wearing": ["brown pants"]}}},
        ]),
        encoding="utf-8",
    )

    result = search_mod.search_shots(
        query="brown pants",
        scopes=None,
        field="wearing",
        limit=None,
        limit_per_item=None,
        use_all=True,
        project_path=str(tmp_path),
        media_type="movie",
    )

    assert [item["shot_id"] for item in result["results"]] == [
        build_shot_id(media_id, 241, 480),
    ]
    assert result["results"][0]["matched_text"] == "brown pants"


def test_search_shots_multiword_query_requires_exact_phrase(tmp_path):
    filename = "Film Two.mp4"
    media_id = "file_film_two"
    save_json_metadata(tmp_path, "movie", [{
        "filename": filename,
        "title": "Film Two",
        "media_id": media_id,
    }])
    write_shotlist(tmp_path, filename, "movie", [
        {
            "Scene": "1",
            "start_time": "00:00:00.000",
            "end_time": "00:00:10.000",
            "start_frame": 0,
            "end_frame": 240,
            "shot_id": build_shot_id(media_id, 0, 240),
        },
        {
            "Scene": "1",
            "start_time": "00:00:10.000",
            "end_time": "00:00:20.000",
            "start_frame": 241,
            "end_frame": 480,
            "shot_id": build_shot_id(media_id, 241, 480),
        },
    ])
    ann_dir = tmp_path / "data" / "annotations" / "shots" / "movie"
    ann_dir.mkdir(parents=True, exist_ok=True)
    (ann_dir / "Film Two.annotations.json").write_text(
        json.dumps([
            {"shot": {"shot_id": build_shot_id(media_id, 0, 240), "annotation": {"wearing": ["black neckerchief"]}}},
            {"shot": {"shot_id": build_shot_id(media_id, 241, 480), "annotation": {"wearing": ["green neckerchief", "black gloves"]}}},
        ]),
        encoding="utf-8",
    )

    result = search_mod.search_shots(
        query="black neckerchief",
        scopes=None,
        field="wearing",
        limit=None,
        limit_per_item=None,
        use_all=True,
        project_path=str(tmp_path),
        media_type="movie",
    )

    assert [item["shot_id"] for item in result["results"]] == [
        build_shot_id(media_id, 0, 240),
    ]
    assert result["results"][0]["matched_text"] == "black neckerchief"
