import json

import pytest
from PyQt5.QtWidgets import QApplication

from generators.cloud import extract_annotation_words
from visualizers.cloud_visualizer import CloudVisualizer


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


def _write_annotations(project_path, media_type, filename, annotations):
    directory = project_path / "data" / "annotations" / "shots" / media_type
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{filename}.annotations.json").write_text(
        json.dumps([{"shot": {"annotation": annotation}} for annotation in annotations]),
        encoding="utf-8",
    )


def test_all_media_cloud_counts_combined_annotations_before_filtering(tmp_path):
    _write_annotations(tmp_path, "movie", "Movie", [{"objects": ["lantern"]}])
    _write_annotations(tmp_path, "gameplay", "Game", [{"objects": ["lantern"]}])

    counts = extract_annotation_words(
        str(tmp_path), field="objects", media_type="--all", min_count=2
    )

    assert counts == {"lantern": 2}


def test_cloud_scope_offers_all_media_titles(tmp_path, app, fake_prefs, monkeypatch):
    monkeypatch.setattr(
        "data.metadata.get_metadata",
        lambda _project_path, media_type: [{
            "title": media_type.title(), "year": "2026", "filename": f"{media_type}.mp4",
        }],
    )
    window = CloudVisualizer(str(tmp_path))
    window.media_combo.setCurrentIndex(0)

    assert window.media_combo.currentText() == "<Media>"
    scopes = [window.movie_combo.itemData(index) for index in range(window.movie_combo.count())]
    assert ("movie", "movie") in scopes
    assert ("gameplay", "gameplay") in scopes
    window.close()