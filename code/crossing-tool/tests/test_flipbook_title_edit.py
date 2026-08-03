"""Smoke test for the Flipbook title field's Enter/Return behavior.

`_on_title_edited()` is wired to `QLineEdit.returnPressed`, so pressing
Enter/Return already saves. The fix under test here is that it must also
*exit editing* afterwards — i.e. release keyboard focus back to the window
— regardless of whether the save succeeded, was a no-op, or raised, via a
try/finally around the save logic.
"""

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from visualizers.flipbook_visualizer import FlipbookVisualizerWindow


@pytest.fixture
def app():
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


@pytest.fixture
def fake_prefs(monkeypatch):
    store: dict = {}
    monkeypatch.setattr("tool.prefs.get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr("tool.prefs.set", lambda key, value: store.__setitem__(key, value))
    return store


@pytest.fixture
def fake_movie(monkeypatch, tmp_path):
    """Stub out the data-layer calls `FlipbookVisualizerWindow` makes so it
    can be constructed with a single fake movie, without touching disk
    beyond one placeholder annotation-json file (only its existence is
    checked)."""
    json_path = tmp_path / "movie.json"
    json_path.write_text("{}")

    monkeypatch.setattr(
        "data.metadata.get_metadata",
        lambda project_path, media_type=None: [
            {"filename": "movie.mp4", "title": "Old Title", "year": 1999}
        ],
    )
    monkeypatch.setattr(
        "data.annotate.get_annotation_json_path",
        lambda project_path, filename, media_type: json_path,
    )
    monkeypatch.setattr(
        "generators.flipbook.load_flipbook_data",
        lambda project_path, filename, media_type="movie": {
            "pages": [],
            "title": "Old Title",
            "year": 1999,
            "filename": filename,
            "film_motif": {"value": "Old Title"},
        },
    )

    saved_titles = []
    monkeypatch.setattr(
        "data.film_motif.set_film_title",
        lambda project_path, filename, media_type, value: saved_titles.append(value),
    )
    return saved_titles


def test_enter_saves_title_and_exits_editing(app, fake_prefs, fake_movie, tmp_path):
    win = FlipbookVisualizerWindow(str(tmp_path), media_type="movie")
    win.show()
    app.processEvents()

    win._title_edit.setFocus()
    assert win._title_edit.hasFocus()

    win._title_edit.setText("New Title")
    win._title_edit.returnPressed.emit()
    app.processEvents()

    assert fake_movie == ["New Title"]
    assert not win._title_edit.hasFocus()

    win.close()


def test_enter_with_empty_title_still_exits_editing(app, fake_prefs, fake_movie, tmp_path):
    win = FlipbookVisualizerWindow(str(tmp_path), media_type="movie")
    win.show()
    app.processEvents()

    win._title_edit.setFocus()
    win._title_edit.setText("   ")
    win._title_edit.returnPressed.emit()
    app.processEvents()

    # No-op save (empty/whitespace-only value): nothing persisted, but
    # focus must still be released from the field.
    assert fake_movie == []
    assert not win._title_edit.hasFocus()

    win.close()
