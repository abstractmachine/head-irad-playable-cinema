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

_REAL_LABEL = "A.T.&S.F. sign"
_NORMALISED_DIR = "a_t__s_f__sign"   # docstring-documented normalised form


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
