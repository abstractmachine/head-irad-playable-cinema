"""Regression tests for IllustrationBrowser.navigate_direct().

Bug: the Illustration visualizer's "Silhouette" button (Engravings tab ->
_jump_to_silhouette -> IllustrationBrowser.navigate_direct) sometimes left
the Silhouettes browser on the wrong page with a stale <Keyword> filter
hiding the target silhouette instead of navigating straight to it.

Root causes (see visualizer-framework.md repo memory for the full writeup):

1. The engraving record's own ``label`` field is the *normalised directory
   name* (e.g. ``"a_t__s_f__sign"``), not the real silhouette label
   (``"A.T.&S.F. sign"``). navigate_direct tried to resolve the real label
   by querying ``source.records(title=item, label=keyword, ...)`` -- using
   the very (possibly wrong) keyword as the pre-filter, which returns zero
   matches whenever it actually needs correcting, so the wrong/normalised
   value stayed active as the Keyword filter and hid the target.
2. Once filters were set, the target was only ever searched for within
   page 0 of the query (offset=0, limit=page_size) -- if its real position
   in the filtered+sorted result set was beyond the first page, it was
   simply never found or selected, leaving the browser on page 0 (or
   whatever stale page was active before) with nothing selected.

These tests exercise the real SQL-backed illustration index
(services.illustration_index + SilhouetteSource) end to end, the same
production code path navigate_direct() uses, rather than a hand-rolled
fake -- for confidence that the fix works against real filter/sort/
pagination semantics (object_id as the canonical per-record identity,
scoped by film/title).

Follow-up: clean semantic browser context
------------------------------------------
A later change further redefined the DESTINATION browser's final filter
state after navigation, on top of (and without altering) the fix above:

    <Media>      left unchanged (see below -- NOT reset to generic)
    <Title>      reset to generic (<Title> / None)
    field        set to the TARGET's own field (data-driven, never hardcoded)
    <Letter>     reset to generic (<Letter> / "--all")
    keyword      set to the TARGET's own real label

regardless of whatever Title/Field/Letter/Keyword filters were active in
either browser beforehand -- the previous incidental browse scope must
never leak into the destination. Field and Keyword are deliberately NOT
reset to generic (they communicate "this exact kind of thing"); only Title
and Letter return to their generic states.

Media is NOT reset to generic despite resembling the other four combos,
because it is not a queryable filter column like Title/Field/Letter/
Keyword -- it is the hard partition that decides which index is even
loaded (see the ``<Media>`` combo construction comment: "'<Media>' means
'nothing selected' (fast empty-browser start)", reload()'s early return
when media_type is falsy, and reset_filters()'s own docstring: "The Media
combo is intentionally left unchanged"). Resetting it would empty the
browser instead of establishing a whole-corpus view. These tests assert
Media stays unchanged, matching reset_filters()'s already-established
convention.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt5.QtWidgets import QApplication

from services.illustration_index import rebuild_index
from visualizers.components.illustration_browser import IllustrationBrowser
from visualizers.components.illustration_source import EngravingSource, SilhouetteSource
from visualizers.components.illustration_source import record_identity
from PyQt5.QtCore import QThread
from PyQt5.QtTest import QSignalSpy
from visualizers.illustration_visualizer import IllustrationPane

_REAL_LABEL = "A.T.&S.F. sign"
_NORMALISED_DIR = "a_t__s_f__sign"   # docstring-documented normalised form


@pytest.fixture
def exact_pane(app, tmp_path, monkeypatch):
    import json
    from tool import prefs
    store = {}
    monkeypatch.setattr(prefs, "get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(prefs, "set", lambda key, value: store.__setitem__(key, value))
    original_init = IllustrationBrowser.__init__

    def unloaded(self, *args, **kwargs):
        kwargs["auto_load"] = False
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(IllustrationBrowser, "__init__", unloaded)
    monkeypatch.setattr(IllustrationBrowser, "_page_size", property(lambda self: 2))
    monkeypatch.setattr(IllustrationPane, "_count_pending_engravings", lambda self: 0)
    stem = "Companeros (1970) {tmdb-61044}"
    silhouettes = []
    for object_number in (1, 3, 4, 5, 2):
        path = tmp_path / "data/silhouettes/catalog/movie" / stem / "bird" / f"object_{object_number:04d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"media_type": "movie", "filename_stem": stem, "label": "bird",
                  "field": "animals", "object_id": path.stem, "path": path,
                  "media_id": "tmdb_61044", "shot_id": "tmdb_61044@f078383-f078476",
                  "frame": 78453, "confidence": 0.5}
        path.write_text(json.dumps({**record, "path": str(path)}))
        silhouettes.append(record)
    other = {**silhouettes[0], "filename_stem": "Other (2000) {tmdb-42}",
             "path": tmp_path / "data/silhouettes/catalog/movie/Other (2000) {tmdb-42}/bird/object_0002.json",
             "object_id": "object_0002"}
    silhouettes.append(other)
    target = silhouettes[4]
    engravings = []
    for mode in ("frame", "isolated"):
        path = tmp_path / "data/engravings/catalog/movie" / stem / "bird/object_0002" / mode / "engraving.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": "generated", "source": {
            "silhouette_json": str(target["path"].relative_to(tmp_path))},
            "silhouette": {key: value for key, value in target.items() if key != "path"}}))
        engravings.append({**target, "path": path, "mode": mode, "status": "generated",
                           "generation_service": "qwen-local", "object_id": "object_0002"})
    with patch("services.illustration_index._scan_silhouettes", return_value=silhouettes):
        rebuild_index(tmp_path, "silhouettes", "movie")
    with patch("services.illustration_index._scan_engravings", return_value=engravings[:1]):
        rebuild_index(tmp_path, "engravings", "movie")
    pane = IllustrationPane(str(tmp_path), media_type="movie")
    yield pane, target, engravings, silhouettes
    for worker in pane.findChildren(QThread):
        if hasattr(worker, "cancel"):
            worker.cancel()
        worker.wait(5000)
    for browser in (pane._browser_sil, pane._browser_eng):
        browser._stop_catalog_loader()
        browser._stop_keyword_loader(wait_ms=5000)
        browser._stop_loader()
        browser._grid_population_timer.stop()
    pane._cancel_relationship_lookup()
    pane._browser_stack.deleteLater()
    pane._side_scroll.deleteLater()
    pane.deleteLater()


def _finish_navigation(browser, action):
    finished = QSignalSpy(browser.navigationFinished)
    action()
    assert finished or finished.wait(5000), "Navigation did not complete"
    assert list(finished[-1]) == [""]


def _finish_relationship(pane):
    loader = pane._relationship_loader
    if loader is not None:
        finished = QSignalSpy(loader.loaded)
        assert finished.wait(5000)


def test_exact_companeros_forward_immediate_and_later_page(exact_pane):
    pane, target, engravings, _ = exact_pane
    pane.select_source_tab("engravings")
    pane._browser_eng._filtered_items = [engravings[0]]
    pane._browser_eng._selected_index = 0
    pane._on_selection_changed(engravings[0])
    browser = pane._browser_sil
    finished = QSignalSpy(browser.navigationFinished)
    pane._eng_sil_btn.click()
    assert pane._browser is browser
    assert pane._current_rec is None
    assert browser._loading_timer.isActive()
    assert finished.wait(5000)
    assert list(finished[-1]) == [""]
    assert browser._media_combo.currentData() == "movie"
    assert browser._item_combo.currentData() == "Companeros (1970)"
    assert browser._field_combo.currentData() == "animals"
    assert browser._letter_combo.currentData() == "B"
    assert browser._keyword_combo.currentData() == "bird"
    assert browser._page_index == 2
    assert browser.currentItem()["path"] == target["path"]
    assert browser._cells[0]._selected
    assert pane._current_rec["object_id"] == "object_0002"
    assert pane._meta_rows["frame"].text().replace("\u200b", "") == "78453"
    assert not browser._loading_timer.isActive()


def test_exact_reverse_and_round_trip(exact_pane):
    pane, target, engravings, _ = exact_pane
    _finish_navigation(pane._browser_sil, lambda: pane.navigate_to_record("silhouettes", record_identity(target, "silhouettes")))
    _finish_relationship(pane)
    assert pane._eng_viz_btn.isEnabled()
    for kind in ("engravings", "silhouettes", "engravings"):
        browser = pane._browser_eng if kind == "engravings" else pane._browser_sil
        action = pane._eng_viz_btn.click if kind == "engravings" else pane._eng_sil_btn.click
        finished = QSignalSpy(browser.navigationFinished)
        action()
        assert pane._browser is browser
        assert pane._current_rec is None
        assert browser._loading_timer.isActive()
        assert finished.wait(5000)
        assert list(finished[-1]) == [""]
        assert pane._browser is browser
        assert browser.currentItem()["path"] == (engravings[0]["path"] if kind == "engravings" else target["path"])
        assert browser._item_combo.currentData() == "Companeros (1970)"
        assert browser._letter_combo.currentData() == "B"
        _finish_relationship(pane)


def test_exact_navigation_atomic_query_budget(exact_pane, monkeypatch):
    import sqlite3
    import threading
    pane, target, _, _ = exact_pane
    statements = []
    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(lambda sql: statements.append((threading.current_thread(), sql)))
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(pane, "_lookup_engravings", lambda rec: None)
    filter_signals = [QSignalSpy(combo.currentIndexChanged) for combo in (
        pane._browser_sil._media_combo, pane._browser_sil._item_combo,
        pane._browser_sil._field_combo, pane._browser_sil._letter_combo,
        pane._browser_sil._keyword_combo,
    )]
    page_calls = []
    monkeypatch.setattr(pane._sil_source, "page", lambda **kwargs: page_calls.append(kwargs))
    _finish_navigation(pane._browser_sil, lambda: pane.navigate_to_record("silhouettes", record_identity(target, "silhouettes")))
    assert not page_calls
    assert all(len(signals) == 0 for signals in filter_signals)
    selects = [sql for _, sql in statements if "FROM records" in sql and "human_best" not in sql]
    assert len(selects) == 7
    assert sum("LIMIT" in sql for sql in selects) == 1
    assert all(thread is not threading.main_thread() for thread, sql in statements if "FROM records" in sql)


def test_exact_new_request_ignores_late_result(exact_pane, monkeypatch, app):
    from visualizers.components.illustration_browser import _CatalogLoader
    pane, target, _, records = exact_pane
    monkeypatch.setattr(_CatalogLoader, "start", lambda self: None)
    browser = pane._browser_sil
    pane.navigate_to_record("silhouettes", record_identity(records[0], "silhouettes"))
    old = browser._catalog_loader
    pane.navigate_to_record("silhouettes", record_identity(target, "silhouettes"))
    current = browser._catalog_loader
    old.result_error = "Old failure"
    old.loaded.emit()
    assert browser._loading_timer.isActive()
    assert browser._catalog_loader is current
    _finish_navigation(browser, current.run)
    old.loaded.emit()
    assert browser.currentItem()["path"] == target["path"]
    assert not browser._loading_timer.isActive()


def test_exact_manual_film_interruption(exact_pane, monkeypatch):
    from visualizers.components.illustration_browser import _CatalogLoader
    pane, target, _, _ = exact_pane
    monkeypatch.setattr(_CatalogLoader, "start", lambda self: None)
    browser = pane._browser_sil
    browser._item_combo.addItem("Other", "Other")
    pane.navigate_to_record("silhouettes", record_identity(target, "silhouettes"))
    old = browser._catalog_loader
    browser._item_combo.setCurrentIndex(browser._item_combo.findData("Other"))
    old.result_error = "Late failure"
    old.loaded.emit()
    assert browser._item_combo.currentData() == "Other"
    assert browser.currentItem() is None
    assert browser._catalog_loader is None


@pytest.mark.parametrize("identity_change, message", [
    ({"object_id": "object_9999"}, "not found"),
    ({"filename_stem": ""}, "Incomplete"),
])
def test_exact_failure_stops_loading(exact_pane, identity_change, message):
    pane, target, _, _ = exact_pane
    browser = pane._browser_sil
    identity = {**record_identity(target, "silhouettes"), **identity_change}
    finished = QSignalSpy(browser.navigationFinished)
    pane.navigate_to_record("silhouettes", identity)
    assert finished.wait(5000)
    assert message in finished[-1][0]
    assert not browser._loading_timer.isActive()
    assert browser.currentItem() is None


def test_multiple_engraving_relationships_need_choice(exact_pane):
    pane, target, engravings, _ = exact_pane
    with patch("services.illustration_index._scan_engravings", return_value=engravings):
        rebuild_index(pane._project_path, "engravings", "movie")
    _finish_navigation(pane._browser_sil, lambda: pane.navigate_to_record("silhouettes", record_identity(target, "silhouettes")))
    _finish_relationship(pane)
    assert len(pane._engraving_choices) == 2
    pane._eng_viz_btn.click()
    assert pane._browser is pane._browser_sil
    assert len(pane._engraving_menu.actions()) == 2
    _finish_navigation(pane._browser_eng, pane._engraving_menu.actions()[1].trigger)
    assert pane._browser_eng.currentItem()["mode"] == "isolated"
    pane._engraving_menu.close()


@pytest.mark.parametrize("failure", ["missing", "archived", "mismatched"])
def test_exact_source_reference_errors(exact_pane, failure):
    import json
    pane, target, engravings, _ = exact_pane
    if failure in ("missing", "archived"):
        if failure == "archived":
            archived = Path(pane._project_path) / "archive/silhouettes/catalog/movie" / target["filename_stem"] / "bird/object_0002.json"
            archived.parent.mkdir(parents=True)
            target["path"].rename(archived)
        else:
            target["path"].unlink()
    else:
        metadata = json.loads(engravings[0]["path"].read_text())
        metadata["source"]["silhouette_json"] = str(target["path"].with_name("object_0001.json"))
        engravings[0]["path"].write_text(json.dumps(metadata))
    browser = pane._browser_sil
    finished = QSignalSpy(browser.navigationFinished)
    pane.navigate_to_record("silhouettes", {"origin": engravings[0], "media_type": "movie"})
    assert finished.wait(5000)
    message = finished[-1][0]
    assert message
    if failure == "archived":
        assert "archived" in message
    assert browser.currentItem() is None
    assert not browser._loading_timer.isActive()
    assert message == pane._meta_rows["label"].text().replace("\u200b", "")


def test_no_published_engraving_disables_button(exact_pane):
    pane, _, _, records = exact_pane
    _finish_navigation(pane._browser_sil, lambda: pane.navigate_to_record("silhouettes", record_identity(records[0], "silhouettes")))
    _finish_relationship(pane)
    assert not pane._eng_viz_btn.isEnabled()
    assert pane._engraving_choices == []


def test_ambiguous_source_never_selects_first(exact_pane):
    pane, target, _, records = exact_pane
    with patch("services.illustration_index._scan_silhouettes", return_value=[*records, target]):
        rebuild_index(pane._project_path, "silhouettes", "movie")
    finished = QSignalSpy(pane._browser_sil.navigationFinished)
    pane.navigate_to_record("silhouettes", record_identity(target, "silhouettes"))
    assert finished.wait(5000)
    assert "Ambiguous" in finished[-1][0]
    assert pane._browser_sil.currentItem() is None


def test_unavailable_index_and_async_exception(exact_pane, monkeypatch):
    pane, target, _, _ = exact_pane
    finished = QSignalSpy(pane._browser_eng.navigationFinished)
    identity = {**record_identity(target, "silhouettes"), "media_type": "gameplay", "mode": "frame"}
    pane.navigate_to_record("engravings", identity)
    assert finished.wait(5000)
    assert "index unavailable" in finished[-1][0]
    assert not pane._browser_eng._loading_timer.isActive()

    def fail(*args):
        raise RuntimeError("Controlled asynchronous failure")

    monkeypatch.setattr(SilhouetteSource, "resolve_destination", fail)
    finished = QSignalSpy(pane._browser_sil.navigationFinished)
    pane.navigate_to_record("silhouettes", record_identity(target, "silhouettes"))
    assert finished.wait(5000)
    assert "Controlled asynchronous failure" in finished[-1][0]
    assert not pane._browser_sil._loading_timer.isActive()


def test_manual_tab_switch_discards_navigation(exact_pane, monkeypatch):
    from visualizers.components.illustration_browser import _CatalogLoader
    pane, target, _, _ = exact_pane
    monkeypatch.setattr(_CatalogLoader, "start", lambda self: None)
    pane.navigate_to_record("silhouettes", record_identity(target, "silhouettes"))
    old = pane._browser_sil._catalog_loader
    pane.select_source_tab("engravings")
    old.result_error = "Late failure"
    old.loaded.emit()
    assert pane._browser is pane._browser_eng
    assert pane._current_rec is None
    assert not pane._browser_sil._loading_timer.isActive()


def test_exact_engraving_later_page_with_current_sort(exact_pane):
    pane, target, engravings, records = exact_pane
    others = [{**engravings[0], "object_id": record["object_id"]} for record in records[:4]]
    with patch("services.illustration_index._scan_engravings", return_value=[*others, engravings[0]]):
        rebuild_index(pane._project_path, "engravings", "movie")
    pane._eng_source.set_sort_keys(["confidence"])
    _finish_navigation(pane._browser_eng, lambda: pane.navigate_to_record("engravings", record_identity(engravings[0], "engravings")))
    assert pane._browser_eng._page_index == 2
    assert pane._browser_eng.currentItem()["object_id"] == "object_0002"
    assert pane._meta_rows["mode"].text().replace("\u200b", "") == "frame"


def test_manual_browsing_after_exact_navigation(exact_pane):
    pane, target, _, _ = exact_pane
    browser = pane._browser_sil
    _finish_navigation(browser, lambda: pane.navigate_to_record("silhouettes", record_identity(target, "silhouettes")))
    for combo, value in ((browser._field_combo, "--all"),
                         (browser._letter_combo, "--all"),
                         (browser._keyword_combo, "bird")):
        presented = QSignalSpy(browser.pageReady)
        index = combo.findData(value)
        assert index >= 0
        if combo.currentIndex() == index:
            combo.currentIndexChanged.emit(index)
        else:
            combo.setCurrentIndex(index)
        assert presented or presented.wait(5000)
    presented = QSignalSpy(browser.pageReady)
    browser._next_btn.click()
    assert presented or presented.wait(5000)
    assert browser._page_index == 1
    browser._on_cell_clicked(0)
    assert pane._current_rec is not None


def test_gameplay_partition_and_manual_movie_switch(exact_pane):
    pane, target, _, _ = exact_pane
    gameplay = {**target, "media_type": "gameplay", "filename_stem": "game_rdr2_demo",
                "media_id": "game_rdr2_demo", "shot_id": "game_rdr2_demo@f000010-f000020"}
    with patch("services.illustration_index._scan_silhouettes", return_value=[gameplay]):
        rebuild_index(pane._project_path, "silhouettes", "gameplay")
    browser = pane._browser_sil
    _finish_navigation(browser, lambda: pane.navigate_to_record("silhouettes", record_identity(gameplay, "silhouettes")))
    assert browser._media_combo.currentData() == "gameplay"
    assert browser.currentItem()["media_id"] == "game_rdr2_demo"
    presented = QSignalSpy(browser.pageReady)
    browser._media_combo.setCurrentIndex(browser._media_combo.findData("movie"))
    assert presented or presented.wait(5000)
    assert browser._media_type == "movie"
    assert all(record["media_type"] == "movie" for record in browser._filtered_items)


def test_navigation_shutdown_cancels_pending_results(exact_pane, monkeypatch):
    from visualizers.components.illustration_browser import _CatalogLoader
    pane, target, _, _ = exact_pane
    monkeypatch.setattr(_CatalogLoader, "start", lambda self: None)
    pane.navigate_to_record("silhouettes", record_identity(target, "silhouettes"))
    worker = pane._browser_sil._catalog_loader
    pane.shutdown_navigation()
    worker.result_error = "After close"
    worker.loaded.emit()
    assert worker._cancelled
    assert pane._browser_sil._catalog_loader is None
    assert pane._current_rec is None
    assert not pane._browser_sil._loading_timer.isActive()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _sign_records(count: int = 10) -> list[dict]:
    """*count* silhouettes sharing the real target label, ascending confidence."""
    return [
        {
            "filename_stem": "film", "field": "signage", "label": _REAL_LABEL,
            "confidence": index / 10.0,
            "path": f"/proj/data/silhouettes/catalog/movie/film/{_NORMALISED_DIR}/object_{index:04d}.json",
        }
        for index in range(count)
    ]


def _distractor_records(label: str, field: str, count: int = 3) -> list[dict]:
    return [
        {
            "filename_stem": "film", "field": field, "label": label,
            "path": f"/proj/data/silhouettes/catalog/movie/film/{label}/object_{index:04d}.json",
        }
        for index in range(count)
    ]


def _build_browser(
    tmp_path, monkeypatch, records, *, page_size=4, sort_keys=None,
    films=("film",), index_status="ready",
):
    """Build a real SilhouetteSource-backed browser, bypassing QThread/reload.

    Mirrors the established `auto_load=False` + manual state-seeding test
    convention (see tests/test_illustration_fast_loading.py) so no real
    background catalog-loader thread needs to run.
    """
    if index_status != "missing":
        with patch("services.illustration_index._scan_silhouettes", return_value=records):
            rebuild_index(tmp_path, "silhouettes", "movie")

    source = SilhouetteSource(str(tmp_path))
    if sort_keys is not None:
        source.set_sort_keys(sort_keys)

    browser = IllustrationBrowser(
        source=source, media_type="movie", auto_load=False, thumb_size=64,
    )
    browser._index_status = (
        {"status": "ready", "usable": True, "count": len(records)}
        if index_status == "ready" else
        {"status": index_status, "count": 0}
    )
    browser._item_combo.addItem("<Title>", userData=None)
    for film in films:
        browser._item_combo.addItem(film, userData=film)

    monkeypatch.setattr(IllustrationBrowser, "_page_size", property(lambda self: page_size))
    return browser


def _target_stem(record: dict) -> str:
    return Path(str(record["path"])).stem


def test_target_already_visible_on_current_page(app, tmp_path, monkeypatch):
    records = _sign_records(10) + _distractor_records("aardvark", "animals")
    browser = _build_browser(tmp_path, monkeypatch, records)
    try:
        target = records[1]   # abs index 1 within the sign label -> page 0
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._page_index == 0
        assert browser._keyword_combo.currentData() == _REAL_LABEL
        assert browser.currentItem()["label"] == _REAL_LABEL
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_target_on_a_later_page(app, tmp_path, monkeypatch):
    records = _sign_records(10)
    browser = _build_browser(tmp_path, monkeypatch, records, page_size=4)
    try:
        target = records[7]   # abs index 7, page_size 4 -> page 1
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._page_index == 1
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_target_on_an_earlier_page_than_browser_was_showing(app, tmp_path, monkeypatch):
    records = _sign_records(10)
    browser = _build_browser(tmp_path, monkeypatch, records, page_size=4)
    try:
        browser._page_index = 3   # stale: browser was showing a later page
        target = records[1]       # real position is page 0
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._page_index == 0
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_keyword_filter_would_exclude_target(app, tmp_path, monkeypatch):
    records = _sign_records(3) + _distractor_records("aardvark", "animals")
    browser = _build_browser(tmp_path, monkeypatch, records)
    try:
        browser._keyword_combo.addItem("<Keyword>", userData="--all")
        browser._keyword_combo.addItem("aardvark", userData="aardvark")
        browser._keyword_combo.setCurrentIndex(1)

        target = records[0]
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._keyword_combo.currentData() == _REAL_LABEL
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_movie_filter_would_exclude_target(app, tmp_path, monkeypatch):
    """Scenario: navigation while Title is filtered to an unrelated film.

    Title must reset to generic in the destination regardless -- it is
    never replayed from either the source browser's prior scope or the
    caller's ``item`` hint (which is a disambiguation aid only).
    """
    records = _sign_records(3)
    other_film = [
        {
            "filename_stem": "other_film", "field": "plants", "label": "cactus",
            "path": "/proj/data/silhouettes/catalog/movie/other_film/cactus/object_0000.json",
        }
    ]
    browser = _build_browser(
        tmp_path, monkeypatch, records + other_film, films=("film", "other_film"),
    )
    try:
        # Browser is currently scoped to a different film entirely.
        idx = browser._item_combo.findData("other_film")
        browser._item_combo.setCurrentIndex(idx)

        target = records[0]
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._item_combo.currentData() is None      # Title resets to generic
        assert browser._field_combo.currentData() == "signage"  # Field = target's own value
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_multiple_filters_active_simultaneously(app, tmp_path, monkeypatch):
    records = (
        _sign_records(3) + _distractor_records("aardvark", "animals")
        + _distractor_records("horse", "animals")
    )
    browser = _build_browser(tmp_path, monkeypatch, records)
    try:
        browser._field_combo.addItem("<Field>", userData="--all")
        browser._field_combo.addItem("animals", userData="animals")
        browser._field_combo.setCurrentIndex(1)
        browser._letter_combo.addItem("<Letter>", userData="--all")
        browser._letter_combo.addItem("H", userData="H")
        browser._letter_combo.setCurrentIndex(1)
        browser._keyword_combo.addItem("<Keyword>", userData="--all")
        browser._keyword_combo.addItem("horse", userData="horse")
        browser._keyword_combo.setCurrentIndex(1)

        target = records[0]
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._item_combo.currentData() is None       # Title resets to generic
        assert browser._field_combo.currentData() == "signage"  # Field = target's own value
        assert browser._letter_combo.currentData() == "--all"
        assert browser._keyword_combo.currentData() == _REAL_LABEL
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_different_sort_order_changes_target_page_consistently(app, tmp_path, monkeypatch):
    records = _sign_records(10)   # confidence ascending with index (0.0 .. 0.9)
    browser = _build_browser(tmp_path, monkeypatch, records, page_size=4, sort_keys=["confidence"])
    try:
        target = records[7]   # confidence 0.7 -> 3rd highest (desc) -> abs index 2 -> page 0
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._page_index == 0
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_target_exists_but_not_on_the_browsers_current_page(app, tmp_path, monkeypatch):
    records = _sign_records(9)
    browser = _build_browser(tmp_path, monkeypatch, records, page_size=4)
    try:
        # A filter compatible with the target's own field was active, but
        # pointed at a stale page -- navigation must still relocate it.
        browser._field_combo.addItem("<Field>", userData="--all")
        browser._field_combo.addItem("signage", userData="signage")
        browser._field_combo.setCurrentIndex(1)
        browser._page_index = 1

        target = records[8]   # abs index 8 -> page 2 with page_size 4
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._page_index == 2
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_target_cannot_be_found(app, tmp_path, monkeypatch):
    records = _sign_records(3)
    browser = _build_browser(tmp_path, monkeypatch, records)
    try:
        browser.navigate_direct(item="film", keyword=None, object_id="object_9999")
        assert browser.currentItem() is None
        assert browser._selected_index == -1
    finally:
        browser.deleteLater()


def test_index_stale_or_unavailable_reports_existing_status(app, tmp_path, monkeypatch):
    records = _sign_records(3)
    browser = _build_browser(tmp_path, monkeypatch, records, index_status="missing")
    try:
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(records[0])
        )
        assert browser.currentItem() is None
        assert browser._selected_index == -1
        assert browser._total_items == 0
        assert browser._page_lbl.text() == "Index missing"
    finally:
        browser.deleteLater()


def test_reported_bug_keyword_active_and_wrong_page_selected(app, tmp_path, monkeypatch):
    """Exact reported reproduction: <Keyword> active (unrelated label) and
    the browser sitting on the wrong page before "Silhouette" is clicked."""
    records = _sign_records(10) + _distractor_records("aardvark", "animals")
    browser = _build_browser(tmp_path, monkeypatch, records, page_size=4)
    try:
        browser._keyword_combo.addItem("<Keyword>", userData="--all")
        browser._keyword_combo.addItem("aardvark", userData="aardvark")
        browser._keyword_combo.setCurrentIndex(1)
        browser._page_index = 5   # arbitrary wrong page

        target = records[7]   # abs index 7 -> page 1 for the resolved keyword
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )

        # Final state must be fully consistent: browser mode/query/page/selection.
        assert browser._item_combo.currentData() is None        # Title resets to generic
        assert browser._field_combo.currentData() == "signage"  # Field = target's own value
        assert browser._letter_combo.currentData() in (None, "--all")
        assert browser._keyword_combo.currentData() == _REAL_LABEL
        assert browser._page_index == 1
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


# ---------------------------------------------------------------------------
# Follow-up: clean semantic browser context (see module docstring)
#
# 12 scenarios: (1) no filters active, (2) Media filtered, (3) Title
# filtered [-> test_movie_filter_would_exclude_target above], (4) Letter
# filtered, (5) Keyword filtered to another value [-> already covered by
# test_keyword_filter_would_exclude_target above], (6) multiple unrelated
# filters active [-> test_multiple_filters_active_simultaneously above],
# (7)-(9) target field is animals/objects/setting, (10) target several
# pages deep in the new field+keyword result set, (11) target on page 0
# disambiguated across films, (12) both directions
# (Illustration(engraving)->Silhouette and Silhouette->Illustration).
# ---------------------------------------------------------------------------

def test_navigation_with_no_filters_active(app, tmp_path, monkeypatch):
    records = _sign_records(3)
    browser = _build_browser(tmp_path, monkeypatch, records)
    try:
        target = records[0]
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._item_combo.currentData() is None
        assert browser._field_combo.currentData() == "signage"
        assert browser._letter_combo.currentData() in (None, "--all")
        assert browser._keyword_combo.currentData() == _REAL_LABEL
        assert browser._page_index == 0
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_navigation_while_media_is_filtered(app, tmp_path, monkeypatch):
    """Media IS established to <All Media> (ALL_MEDIA) by navigate_direct().

    <All Media> is a real, active cross-media query state -- not an
    "uninitialized/empty" one -- so navigate_direct() always establishes it,
    regardless of the browser's prior single-media-type scope. See
    services.illustration_index.ALL_MEDIA / MEDIA_TYPES.
    """
    from services.illustration_index import ALL_MEDIA
    records = _sign_records(3)
    browser = _build_browser(tmp_path, monkeypatch, records)
    try:
        assert browser._media_combo.currentData() == "movie"   # construction default

        target = records[0]
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._media_combo.currentData() == ALL_MEDIA
        assert browser._item_combo.currentData() is None
        assert browser._field_combo.currentData() == "signage"
        assert browser._keyword_combo.currentData() == _REAL_LABEL
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_navigation_while_letter_is_filtered(app, tmp_path, monkeypatch):
    records = _sign_records(3)
    browser = _build_browser(tmp_path, monkeypatch, records)
    try:
        browser._letter_combo.addItem("<Letter>", userData="--all")
        browser._letter_combo.addItem("H", userData="H")
        browser._letter_combo.setCurrentIndex(1)

        target = records[0]
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=_target_stem(target)
        )
        assert browser._letter_combo.currentData() == "--all"   # Letter resets to generic
        assert browser._item_combo.currentData() is None
        assert browser._field_combo.currentData() == "signage"
        assert browser._keyword_combo.currentData() == _REAL_LABEL
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_target_field_is_animals(app, tmp_path, monkeypatch):
    records = _distractor_records("horse", "animals", count=5) + _sign_records(3)
    browser = _build_browser(tmp_path, monkeypatch, records)
    try:
        target = records[2]   # one of the "horse"/"animals" records
        browser.navigate_direct(
            item="film", keyword="horse", object_id=_target_stem(target)
        )
        assert browser._item_combo.currentData() is None
        assert browser._field_combo.currentData() == "animals"
        assert browser._letter_combo.currentData() in (None, "--all")
        assert browser._keyword_combo.currentData() == "horse"
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_target_field_is_objects(app, tmp_path, monkeypatch):
    records = _distractor_records("bag", "objects", count=5) + _sign_records(3)
    browser = _build_browser(tmp_path, monkeypatch, records)
    try:
        target = records[1]
        browser.navigate_direct(
            item="film", keyword="bag", object_id=_target_stem(target)
        )
        assert browser._item_combo.currentData() is None
        assert browser._field_combo.currentData() == "objects"
        assert browser._keyword_combo.currentData() == "bag"
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_target_field_is_setting(app, tmp_path, monkeypatch):
    records = _distractor_records("tavern", "setting", count=5) + _sign_records(3)
    browser = _build_browser(tmp_path, monkeypatch, records)
    try:
        target = records[0]
        browser.navigate_direct(
            item="film", keyword="tavern", object_id=_target_stem(target)
        )
        assert browser._item_combo.currentData() is None
        assert browser._field_combo.currentData() == "setting"
        assert browser._keyword_combo.currentData() == "tavern"
        assert _target_stem(browser.currentItem()) == _target_stem(target)
    finally:
        browser.deleteLater()


def test_target_several_pages_deep_across_films_in_new_result_set(app, tmp_path, monkeypatch):
    """The new result set is scoped to the target's field+keyword across the
    WHOLE corpus (title=None), not just the source film -- pagination must
    be computed against that combined set, never just one film's slice."""
    film_a_horses = _distractor_records("horse", "animals", count=10)
    for i, record in enumerate(film_a_horses):
        record["filename_stem"] = "film_a"
        record["path"] = f"/proj/data/silhouettes/catalog/movie/film_a/horse/object_{i:04d}.json"
    film_b_horses = _distractor_records("horse", "animals", count=5)
    for i, record in enumerate(film_b_horses):
        record["filename_stem"] = "film_b"
        record["path"] = f"/proj/data/silhouettes/catalog/movie/film_b/horse/object_{i:04d}.json"

    records = film_a_horses + film_b_horses
    browser = _build_browser(
        tmp_path, monkeypatch, records, page_size=4, films=("film_a", "film_b"),
    )
    try:
        # film_a's horses occupy combined abs indices 0-9; object_0007 is
        # the 8th match -> page 1 with page_size 4 (not page 0).
        target = film_a_horses[7]
        browser.navigate_direct(
            item="film_a", keyword="horse", object_id=_target_stem(target)
        )
        assert browser._page_index == 1
        assert browser._field_combo.currentData() == "animals"
        assert browser._keyword_combo.currentData() == "horse"
        assert _target_stem(browser.currentItem()) == _target_stem(target)
        assert browser.currentItem()["filename_stem"] == "film_a"
    finally:
        browser.deleteLater()


def test_target_on_page_0_disambiguated_across_films(app, tmp_path, monkeypatch):
    """film_b shares an identically-numbered object_id in the same
    field+keyword group -- navigating with item="film_a" must select
    film_a's object, never film_b's same-numbered one."""
    film_a_horses = _distractor_records("horse", "animals", count=10)
    for i, record in enumerate(film_a_horses):
        record["filename_stem"] = "film_a"
        record["path"] = f"/proj/data/silhouettes/catalog/movie/film_a/horse/object_{i:04d}.json"
    film_b_horses = _distractor_records("horse", "animals", count=5)
    for i, record in enumerate(film_b_horses):
        record["filename_stem"] = "film_b"
        record["path"] = f"/proj/data/silhouettes/catalog/movie/film_b/horse/object_{i:04d}.json"

    records = film_a_horses + film_b_horses
    browser = _build_browser(
        tmp_path, monkeypatch, records, page_size=4, films=("film_a", "film_b"),
    )
    try:
        target = film_a_horses[1]   # abs index 1 -> page 0
        browser.navigate_direct(
            item="film_a", keyword="horse", object_id=_target_stem(target)
        )
        assert browser._page_index == 0
        assert _target_stem(browser.currentItem()) == _target_stem(target)
        assert browser.currentItem()["filename_stem"] == "film_a"
    finally:
        browser.deleteLater()


def test_bidirectional_silhouette_to_engraving(app, tmp_path, monkeypatch):
    """Mirrors _visualize_engraving(): keyword arrives as the silhouette's
    own label-directory name, which for engravings IS the real label
    verbatim (no correction needed); Field stays generic since engravings
    have no field taxonomy (services.illustration_index._scan_engravings
    always sets field="--all")."""
    eng_records = [
        {
            "filename_stem": "film", "field": "--all", "label": _NORMALISED_DIR,
            "mode": "isolated", "object_id": f"object_{index:04d}",
            "path": (
                f"/proj/data/engravings/catalog/movie/film/{_NORMALISED_DIR}"
                f"/object_{index:04d}/isolated/engraving.json"
            ),
        }
        for index in range(6)
    ]
    with patch("services.illustration_index._scan_engravings", return_value=eng_records):
        rebuild_index(tmp_path, "engravings", "movie")
    source = EngravingSource(str(tmp_path))
    browser = IllustrationBrowser(
        source=source, media_type="movie", auto_load=False, thumb_size=64,
    )
    browser._index_status = {"status": "ready", "usable": True, "count": len(eng_records)}
    browser._item_combo.addItem("<Title>", userData=None)
    browser._item_combo.addItem("film", userData="film")
    monkeypatch.setattr(IllustrationBrowser, "_page_size", property(lambda self: 4))
    try:
        target = eng_records[4]   # abs index 4 -> page 1 with page_size 4
        browser.navigate_direct(
            item="film", keyword=_NORMALISED_DIR, object_id=target["object_id"],
        )
        assert browser._item_combo.currentData() is None
        assert browser._field_combo.currentData() == "--all"     # no field taxonomy
        assert browser._letter_combo.currentData() in (None, "--all")
        assert browser._keyword_combo.currentData() == _NORMALISED_DIR
        assert browser._page_index == 1
        assert browser.currentItem()["object_id"] == target["object_id"]
    finally:
        browser.deleteLater()
