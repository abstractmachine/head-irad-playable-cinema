import json
import subprocess

import pytest
from PyQt5.QtWidgets import QApplication

import services.search as search_mod
import services.vocabulary_index as vocabulary_index_mod
import visualizers.mosaic_visualizer as mosaic_mod
from data.index import save_vocabulary_fields
from services.vocabulary_index import build_vocabulary_index
from visualizers.mosaic_visualizer import (
    MosaicVisualizer,
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
        "--all", str(tmp_path), "movie", sort="count"
    )

    assert result == {
        "status": "ready",
        "items": [
            {"value": "horse", "count": 2},
            {"value": "saddle", "count": 1},
        ],
    }


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
        "--force",
    ]
    assert kwargs == {"capture_output": True, "text": True, "check": False}
    assert outputs == ["Saved: vocabulary_movie.json"]


def test_mosaic_inspector_shows_stale_and_rebuild_busy_states(
    tmp_path, monkeypatch, app, fake_prefs
):
    monkeypatch.setattr(VocabularyIndexWorker, "start", lambda self: None)
    window = MosaicVisualizer(str(tmp_path))

    window._on_vocab_result({"status": "stale"})
    assert window.vocab_table._rows[0].text() == (
        "Vocabulary index is stale. Rebuild it to continue."
    )

    window._on_vocab_rebuild()
    assert not window.vocab_rebuild_btn.isEnabled()
    assert window.vocab_rebuild_btn.text() == "Rebuilding..."
    assert window.vocab_table._rows[0].text() == "Rebuilding vocabulary index..."

    window._on_vocab_rebuild_error("command failed")
    assert window.vocab_rebuild_btn.isEnabled()
    assert window.vocab_rebuild_btn.text() == "Rebuild Vocabulary"
    assert window.vocab_table._rows[0].text() == "Rebuild failed: command failed"
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
    assert window.vocab_nav_combo.currentText() == "<all>"
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

    assert window.field_combo.currentText() == "<all>"
    assert window.field_combo.currentData() == "--all"
    assert started_fields == ["--all"]
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
