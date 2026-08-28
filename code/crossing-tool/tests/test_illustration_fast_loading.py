from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt5.QtWidgets import QApplication

from visualizers.components.illustration_browser import (
    IllustrationBrowser,
    _CatalogLoader,
    _KeywordLoader,
)
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.illustration_source import IllustrationSource
from visualizers.illustration_visualizer import IllustrationPane, _IllustrationIndexWorker


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _MemorySource(IllustrationSource):
    def _load(self, media_type: str) -> list[dict]:
        self._load_status = {"status": "ready"}
        return []

    def thumbnail_path(self, record: dict):
        return None


def _make_pane(tmp_path):
    preferences = {}

    with (
        patch("tool.prefs.get", side_effect=lambda key, default=None: preferences.get(key, default)),
        patch("tool.prefs.set", side_effect=lambda key, value: preferences.__setitem__(key, value)),
    ):
        pane = IllustrationPane(str(tmp_path), media_type=None)

    return pane


def _visible_text(widget) -> str:
    return widget.text().replace("\u200b", "")


def _provenance_record(label: str, provenance: dict | None) -> dict:
    record = {
        "filename_stem": "film",
        "field": "objects",
        "label": label,
    }
    if provenance is not None:
        record["search_provenance"] = provenance
    return record


def test_grid_cells_are_created_in_bounded_batches(app, tmp_path):
    browser = IllustrationBrowser(_MemorySource(str(tmp_path)), media_type=None)
    browser._filtered_items = [
        {"label": f"item-{index}", "filename_stem": "film"}
        for index in range(45)
    ]
    browser._current_page_records = lambda: list(browser._filtered_items)

    browser._rebuild_grid()
    browser._grid_population_timer.stop()
    assert browser._cells == []
    assert browser._grid_widget.isHidden()

    browser._append_cell_batch()
    browser._grid_population_timer.stop()
    assert len(browser._cells) == 20
    assert browser._grid_widget.isHidden()

    browser._append_cell_batch()
    browser._grid_population_timer.stop()
    assert len(browser._cells) == 40

    browser._append_cell_batch()
    assert len(browser._cells) == 45
    assert not browser._grid_widget.isHidden()
    browser._stop_loader()
    browser.deleteLater()


def test_index_worker_rebuilds_both_sources(app, tmp_path):
    results = {
        "silhouettes": {"status": "ready", "count": 4},
        "engravings": {"status": "ready", "count": 2},
    }
    emitted = []
    worker = _IllustrationIndexWorker(str(tmp_path), "movie")
    worker.finished.connect(lambda ok, error: emitted.append((ok, error)))

    with patch("services.illustration_index.rebuild_all", return_value=results) as rebuild:
        worker.run()

    rebuild.assert_called_once_with(str(tmp_path), "movie")
    assert emitted == [(True, "")]


def test_live_pane_has_rebuild_action_on_both_tabs(app, tmp_path):
    preferences = {}

    def pref_get(key, default=None):
        return preferences.get(key, default)

    with (
        patch("tool.prefs.get", side_effect=pref_get),
        patch("tool.prefs.set", side_effect=lambda key, value: preferences.__setitem__(key, value)),
    ):
        pane = IllustrationPane(str(tmp_path), media_type=None)

    assert pane._sil_rebuild_index_btn.text() == "Rebuild Index"
    assert pane._eng_rebuild_index_btn.text() == "Rebuild Index"
    assert not pane._sil_rebuild_index_btn.isEnabled()
    assert not pane._eng_rebuild_index_btn.isEnabled()

    pane._browser_sil._index_status = {"status": "stale"}
    pane._browser_eng._index_status = {"status": "ready"}
    pane._update_rebuild_index_buttons()

    assert pane._sil_rebuild_index_btn.isEnabled()
    assert not pane._eng_rebuild_index_btn.isEnabled()

    pane._browser_sil._index_status = {"status": "loading"}
    pane._browser_eng._index_status = {"status": "missing"}
    pane._update_rebuild_index_buttons()

    assert not pane._sil_rebuild_index_btn.isEnabled()
    assert pane._eng_rebuild_index_btn.isEnabled()
    pane.deleteLater()


def test_provenance_section_exists_and_defaults_to_all(app, tmp_path):
    pane = _make_pane(tmp_path)

    assert isinstance(pane._sil_provenance_section, CollapsibleSection)
    assert pane._sil_provenance_section._title == "Provenance"
    assert pane._sil_provenance_section._pref_key == "ill_sil_section_provenance"
    assert pane._sil_provenance_combo.currentData() is None
    assert pane._sil_provenance_combo.itemText(0) == "All"
    assert pane._sil_provenance_combo.itemText(1) == "✓ Valid"
    assert pane._sil_provenance_combo.itemText(2) == "? Questionable"
    assert pane._sil_provenance_combo.property("crossingCanonicalCombo") is True
    assert pane._sil_provenance_section._body_layout.count() == 1
    assert pane._sil_provenance_section._body_layout.itemAt(0).widget() is pane._sil_provenance_combo
    pane.deleteLater()


def test_provenance_filter_updates_source_state_without_catalog_scan(app, tmp_path):
    pane = _make_pane(tmp_path)

    with (
        patch.object(pane._browser_sil, "reload") as reload_browser,
        patch("services.silhouette_catalog.scan_catalog", side_effect=AssertionError("scan_catalog should not be called")),
    ):
        valid_idx = pane._sil_provenance_combo.findData("valid")
        pane._sil_provenance_combo.setCurrentIndex(valid_idx)
        assert pane._sil_source._provenance_state == "valid"
        reload_browser.assert_called_once()

        questionable_idx = pane._sil_provenance_combo.findData("questionable")
        pane._sil_provenance_combo.setCurrentIndex(questionable_idx)
        assert pane._sil_source._provenance_state == "questionable"
        assert reload_browser.call_count == 2

    pane.deleteLater()


def test_provenance_details_render_questionable_and_update_on_selection_change(app, tmp_path):
    pane = _make_pane(tmp_path)

    questionable = _provenance_record(
        "adobe building",
        {
            "state": "questionable",
            "reason": "multi_word_not_exact_annotation_value",
            "audit_version": "semantic-v1",
            "audit_classification": "QUESTIONABLE_PARTIAL",
            "annotation_values": ["gate", "building", "lantern"],
            "matched_words": ["building"],
            "missing_words": ["adobe"],
            "support_values": {"adobe": [], "building": ["building"]},
            "exact_annotation_match": False,
            "all_words_present": False,
            "all_words_present_as_one_value": False,
            "separate_component_values": False,
        },
    )
    valid_exact = _provenance_record(
        "yellow coat",
        {
            "state": "valid",
            "reason": "exact_annotation_value",
            "audit_version": "semantic-v1",
            "audit_classification": "VALID_EXACT",
            "annotation_values": ["yellow coat"],
            "matched_words": ["yellow", "coat"],
            "missing_words": [],
            "support_values": {"yellow": ["yellow coat"], "coat": ["yellow coat"]},
            "exact_annotation_match": True,
            "all_words_present": True,
            "all_words_present_as_one_value": True,
            "separate_component_values": False,
        },
    )

    with patch("services.silhouette_catalog.scan_catalog", side_effect=AssertionError("scan_catalog should not be called")):
        pane._on_selection_changed(questionable)
        assert pane._sil_provenance_details_section._header.text() == "Provenance: ? Questionable"
        assert _visible_text(pane._sil_provenance_details_rows["Original annotation"]) == "gate, building, lantern"
        assert _visible_text(pane._sil_provenance_details_rows["Matched"]) == "building"
        assert _visible_text(pane._sil_provenance_details_rows["Missing"]) == "adobe"
        assert _visible_text(pane._sil_provenance_details_rows["Classification"]) == "QUESTIONABLE_PARTIAL"

        pane._on_selection_changed(valid_exact)
        assert pane._sil_provenance_details_section._header.text() == "Provenance: ✓ Valid"
        assert _visible_text(pane._sil_provenance_details_rows["Original annotation"]) == "yellow coat"
        assert _visible_text(pane._sil_provenance_details_rows["Matched"]) == "yellow, coat"
        assert _visible_text(pane._sil_provenance_details_rows["Missing"]) == "—"
        assert _visible_text(pane._sil_provenance_details_rows["Classification"]) == "VALID_EXACT"

    pane.deleteLater()


def test_provenance_details_render_valid_single_and_clear_on_missing_provenance(app, tmp_path):
    pane = _make_pane(tmp_path)

    valid_single = _provenance_record(
        "coat",
        {
            "state": "valid",
            "reason": "single_word_label",
            "audit_version": "semantic-v1",
            "audit_classification": "VALID_SINGLE",
            "annotation_values": ["coat", "shirt", "boots"],
            "matched_words": ["coat"],
            "missing_words": [],
            "support_values": {"coat": ["coat"], "shirt": ["shirt"], "boots": ["boots"]},
            "exact_annotation_match": True,
            "all_words_present": False,
            "all_words_present_as_one_value": False,
            "separate_component_values": False,
        },
    )
    missing = _provenance_record("coat", None)

    pane._on_selection_changed(valid_single)
    assert pane._sil_provenance_details_section._header.text() == "Provenance: ✓ Valid"
    assert _visible_text(pane._sil_provenance_details_rows["Original annotation"]) == "coat, shirt, boots"
    assert _visible_text(pane._sil_provenance_details_rows["Matched"]) == "coat"
    assert _visible_text(pane._sil_provenance_details_rows["Missing"]) == "—"
    assert _visible_text(pane._sil_provenance_details_rows["Classification"]) == "VALID_SINGLE"

    pane._on_selection_changed(missing)
    assert pane._sil_provenance_details_section._header.text() == "Provenance: —"
    assert _visible_text(pane._sil_provenance_details_rows["Original annotation"]) == "—"
    assert _visible_text(pane._sil_provenance_details_rows["Matched"]) == "—"
    assert _visible_text(pane._sil_provenance_details_rows["Missing"]) == "—"
    assert _visible_text(pane._sil_provenance_details_rows["Classification"]) == "—"

    pane.deleteLater()


def test_hidden_engraving_source_loads_on_first_tab_activation(app, tmp_path):
    preferences = {}

    with (
        patch("tool.prefs.get", side_effect=lambda key, default=None: preferences.get(key, default)),
        patch("tool.prefs.set", side_effect=lambda key, value: preferences.__setitem__(key, value)),
        patch.object(IllustrationBrowser, "reload") as reload_catalog,
    ):
        pane = IllustrationPane(str(tmp_path), media_type="movie")
        app.processEvents()

        assert pane._browser_eng._load_requested is False
        assert reload_catalog.call_count == 1
        pane._side_scroll.tabbed_panel().setCurrentIndex(1)

        assert reload_catalog.call_count == 2
        pane._browser_eng._load_requested = True
        pane._side_scroll.tabbed_panel().setCurrentIndex(0)
        pane._side_scroll.tabbed_panel().setCurrentIndex(1)
        assert reload_catalog.call_count == 2

    pane.deleteLater()


def test_catalog_loader_uses_facets_from_index(app, tmp_path):
    source = _MemorySource(str(tmp_path))
    source._load = lambda media_type: [{
        "filename_stem": "film", "field": "animals", "label": "horse"
    }]
    loader = _CatalogLoader(source, "movie")

    with patch.object(loader, "_build_filter_cache", side_effect=AssertionError):
        loader.run()

    assert loader.result_cache == {
        "films": ["film"],
        "fields": {"animals"},
        "letters": ["H"],
        "counts": {"horse": 1},
    }


def test_keyword_worker_returns_counts_and_scoped_records(app):
    records = [
        {"filename_stem": "film", "field": "animals", "label": "horse"},
        {"filename_stem": "film", "field": "animals", "label": "hare"},
        {"filename_stem": "film", "field": "objects", "label": "hat"},
        {"filename_stem": "other", "field": "animals", "label": "horse"},
    ]
    worker = _KeywordLoader(records, "film", "animals", "H")

    worker.run()

    assert worker.result_records == records[:2]
    assert worker.result_counts == {"horse": 1, "hare": 1}
    assert worker.result_labels == ["hare", "horse"]


def test_keyword_combo_load_is_animated_and_batched(app, tmp_path):
    browser = IllustrationBrowser(
        _MemorySource(str(tmp_path)), media_type=None, auto_load=False
    )
    browser._all_items = [
        {"filename_stem": "film", "field": "animals", "label": f"h{index:03d}"}
        for index in range(205)
    ]
    browser._source._records = list(browser._all_items)
    browser._item_combo.addItem("film", userData="film")
    browser._item_combo.setCurrentIndex(1)
    browser._field_combo.addItem("animals", userData="animals")
    browser._field_combo.setCurrentIndex(1)
    browser._letter_combo.addItem("H", userData="H")
    browser._letter_combo.setCurrentIndex(1)

    with patch.object(_KeywordLoader, "start"):
        browser._rebuild_keyword_combo()

    assert browser._loading_bar._active is True
    assert browser._loading_timer.isActive()
    assert browser._keyword_combo.isEnabled() is False

    worker = browser._keyword_loader
    worker.run()
    browser._on_keywords_loaded()
    browser._keyword_population_timer.stop()

    browser._append_keyword_batch()
    browser._keyword_population_timer.stop()
    assert browser._keyword_combo.count() == 101
    assert browser._keyword_combo.isEnabled() is False

    browser._append_keyword_batch()
    browser._keyword_population_timer.stop()
    assert browser._keyword_combo.count() == 201

    browser._append_keyword_batch()
    assert browser._keyword_combo.count() == 206
    assert browser._keyword_combo.isEnabled() is True
    assert browser._keyword_scope_items is None
    assert browser._loading_bar._active is True

    browser._grid_population_timer.stop()
    browser._stop_loader()
    browser.deleteLater()


def test_stale_keyword_worker_result_is_ignored(app, tmp_path):
    browser = IllustrationBrowser(
        _MemorySource(str(tmp_path)), media_type=None, auto_load=False
    )
    current = _KeywordLoader([], None, "--all", "--all", parent=browser)
    stale = _KeywordLoader([], None, "--all", "A", parent=browser)
    browser._keyword_loader = current

    with patch.object(browser, "sender", return_value=stale):
        browser._on_keywords_loaded()

    assert browser._keyword_loader is current
    assert browser._keyword_population_timer.isActive() is False
    browser._keyword_loader = None
    browser.deleteLater()


def test_item_filter_change_clears_lower_filters_and_grid_before_rebuilding(app, tmp_path):
    browser = IllustrationBrowser(
        _MemorySource(str(tmp_path)), media_type=None, auto_load=False,
    )
    browser._item_combo.addItem("film", userData="film")
    browser._item_combo.setCurrentIndex(1)
    browser._field_combo.addItem("animals", userData="animals")
    browser._field_combo.setCurrentIndex(1)
    browser._letter_combo.addItem("H", userData="H")
    browser._letter_combo.setCurrentIndex(1)
    browser._keyword_combo.addItem("horse  (1)", userData="horse")
    browser._keyword_combo.setCurrentIndex(1)
    browser._filtered_items = [{"label": "horse", "filename_stem": "film"}]
    browser._current_page_records = lambda: list(browser._filtered_items)
    browser._rebuild_grid()
    browser._grid_population_timer.stop()
    browser._append_cell_batch()
    assert len(browser._cells) == 1

    with patch.object(browser, "_rebuild_field_combo") as rebuild:
        browser._on_item_changed(browser._item_combo.currentIndex())

    assert rebuild.called
    assert [browser._field_combo.itemData(index) for index in range(browser._field_combo.count())] == ["--all"]
    assert [browser._letter_combo.itemData(index) for index in range(browser._letter_combo.count())] == ["--all"]
    assert [browser._keyword_combo.itemData(index) for index in range(browser._keyword_combo.count())] == ["--all"]
    assert not browser._field_combo.isEnabled()
    assert not browser._letter_combo.isEnabled()
    assert not browser._keyword_combo.isEnabled()
    assert browser._cells == []
    assert browser._total_items == 0
    browser.deleteLater()


def test_keyword_navigation_retries_until_async_population_finishes(app, tmp_path):
    browser = IllustrationBrowser(
        _MemorySource(str(tmp_path)), media_type=None, auto_load=False
    )
    browser._keyword_loader = _KeywordLoader([], None, "--all", "H", parent=browser)
    browser._keyword_combo.setEnabled(False)
    retries = []

    with patch(
        "visualizers.components.illustration_browser.QTimer.singleShot",
        side_effect=lambda delay, callback: retries.append((delay, callback)),
    ):
        browser.navigate_to_filters(keyword="horse")

    assert retries[0][0] == 50

    browser._keyword_loader = None
    browser._keyword_combo.setEnabled(True)
    browser._keyword_combo.addItem("horse  (2)", userData="horse")
    retries[0][1]()
    assert browser._keyword_combo.currentData() == "horse"
    browser.deleteLater()


def test_pagination_distinguishes_loading_from_index_errors(app, tmp_path):
    browser = IllustrationBrowser(
        _MemorySource(str(tmp_path)), media_type="movie", auto_load=False
    )

    browser._index_status = {"status": "loading"}
    browser._update_pagination()
    assert browser._page_lbl.text() == "Loading"

    browser._index_status = {"status": "missing"}
    browser._update_pagination()
    assert browser._page_lbl.text() == "Index missing"

    browser._index_status = {"status": "stale"}
    browser._update_pagination()
    assert browser._page_lbl.text() == "Index stale"

    browser._index_status = {"status": "ready"}
    browser._update_pagination()
    assert browser._page_lbl.text() == "No items"

    browser._index_status = {"status": "stale", "usable": True}
    browser._total_items = 25
    browser._update_pagination()
    assert browser._page_lbl.text().startswith("Stale · 1 /")
    browser.deleteLater()


def test_reload_sets_loading_before_catalog_worker_finishes(app, tmp_path):
    browser = IllustrationBrowser(
        _MemorySource(str(tmp_path)), media_type="movie", auto_load=False
    )

    with patch.object(_CatalogLoader, "start"):
        browser.reload()

    assert browser._index_status == {"status": "loading"}
    assert browser._page_lbl.text() == "Loading"
    browser._stop_catalog_loader()
    browser.deleteLater()


def test_rebuild_reload_keeps_inactive_source_lazy(app, tmp_path):
    preferences = {}
    with (
        patch("tool.prefs.get", side_effect=lambda key, default=None: preferences.get(key, default)),
        patch("tool.prefs.set", side_effect=lambda key, value: preferences.__setitem__(key, value)),
    ):
        pane = IllustrationPane(str(tmp_path), media_type="movie")

    pane._browser_eng._load_requested = True
    with (
        patch.object(pane._browser_sil, "reload") as reload_silhouettes,
        patch.object(pane._browser_eng, "reload") as reload_engravings,
    ):
        pane._on_index_rebuild_finished(True, "")

    reload_silhouettes.assert_called_once_with()
    reload_engravings.assert_not_called()
    assert pane._browser_eng._load_requested is False
    pane.deleteLater()