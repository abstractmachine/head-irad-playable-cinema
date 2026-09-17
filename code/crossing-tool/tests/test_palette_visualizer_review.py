import pytest
from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QColor, QKeyEvent
from PyQt5.QtWidgets import QApplication, QLineEdit

from data import palette_review as store


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    """A real Palette window over fake media.

    Both ``tool.prefs.get`` and ``tool.prefs.set`` are sandboxed — a real
    ``set()`` deep inside a section toggle would otherwise write to the user's
    actual preferences file.
    """
    values = {}
    monkeypatch.setattr("tool.prefs.get", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr("tool.prefs.set", lambda key, value: values.__setitem__(key, value))

    films = {
        "movie": [{"filename": "first.mkv", "title": "First", "year": "1956"},
                  {"filename": "second.mkv", "title": "Second", "year": "1960"}],
        "gameplay": [{"filename": "clip.mp4", "title": "Clip", "year": ""}],
    }
    monkeypatch.setattr("data.metadata.get_metadata",
                        lambda project, media_type="movie", **kw: films.get(media_type, []))

    def _frames(project, filename, media_type):
        return [
            {"index": index, "shot_id": f"{filename}@{index}", "scene": "1",
             "start_time": "", "image": str(tmp_path / "missing.png"),
             "available": False}
            for index in range(4)
        ]

    monkeypatch.setattr("services.palette_review.list_frames", _frames)

    from visualizers.palette_visualizer import PaletteVisualizerWindow

    win = PaletteVisualizerWindow(str(tmp_path))
    win._load_films()
    yield win
    win.close()


def _press(window, key, text="", modifiers=Qt.NoModifier):
    window.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, modifiers, text))


def _double_click(x=10, y=10):
    from PyQt5.QtCore import QPoint
    from PyQt5.QtGui import QMouseEvent

    return QMouseEvent(QEvent.MouseButtonDblClick, QPoint(x, y),
                       Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)


def _generated_entry(choices=("1", "2")):
    """A standalone generated frame record, for pure store-level assertions."""
    from services import palette_review as service

    return store.record_generation(
        {}, service.PROPOSAL_VERSION, "gen-x",
        {key: {"strategy": "direct", "label": "DIRECT", "active": True,
               "materials": ["a", "b"], "converges_with": [],
               "colours": [{"rgb": [1, 2, 3], "hex": "#010203"},
                           {"rgb": [4, 5, 6], "hex": "#040506"}]}
         for key in choices},
        {"source_image": "x.png"},
    )


def _generated(window, choices=("1", "2")):
    """Give the current frame stored proposals, through the real store.

    Each choice gets its own colours so a split between two of them is
    visible in the resulting palette, not just in the recorded choice.
    """
    from services import palette_review as service

    record = store.load_review(window._project_path, window._filename,
                               window._media_type)
    entry = store.frame(record, window.current_frame()["shot_id"])
    store.record_generation(
        entry, service.PROPOSAL_VERSION, "gen-test",
        {key: {"strategy": f"strategy-{key}", "label": "DIRECT", "active": True,
               "materials": ["a", "b"], "converges_with": [],
               "colours": [{"rgb": [int(key), 2, 3],
                           "hex": "#{:02x}0203".format(int(key))},
                           {"rgb": [int(key), 5, 6],
                            "hex": "#{:02x}0506".format(int(key))}]}
         for key in choices},
        {"source_image": "x.png"},
    )
    store.save_review(window._project_path, window._filename,
                      window._media_type, record)
    window.reload()


class TestMediaSelection:
    def test_the_default_media_type_is_movie(self, window):
        assert window._media_type == "movie"
        assert window._media_combo.currentText() == "movie"

    def test_the_canonical_vocabulary_has_no_plural_forms(self, window):
        items = [window._media_combo.itemText(index)
                 for index in range(window._media_combo.count())]
        assert items == ["movie", "gameplay"]

    def test_the_first_movie_is_selected_by_default(self, window):
        assert window._filename == "first.mkv"
        assert window._film_combo.currentIndex() == 0

    def test_changing_media_type_reloads_its_own_titles(self, window):
        window._media_combo.setCurrentText("gameplay")
        assert window._media_type == "gameplay"
        assert window._filename == "clip.mp4"

    def test_changing_title_reloads_frames(self, window):
        window._film_combo.setCurrentIndex(1)
        assert window._filename == "second.mkv"
        assert window.current_frame()["shot_id"].startswith("second.mkv@")


class TestNavigation:
    def test_right_arrow_moves_forward(self, window):
        _press(window, Qt.Key_Right)
        assert window._current == 1

    def test_left_arrow_moves_backward(self, window):
        _press(window, Qt.Key_Right)
        _press(window, Qt.Key_Right)
        _press(window, Qt.Key_Left)
        assert window._current == 1

    def test_navigation_stops_at_the_ends(self, window):
        _press(window, Qt.Key_Left)
        assert window._current == 0
        for _ in range(10):
            _press(window, Qt.Key_Right)
        assert window._current == len(window._frames) - 1

    def test_s_enters_single_frame(self, window):
        _press(window, Qt.Key_A, "a")
        _press(window, Qt.Key_S, "s")
        assert window.mode() == "single"

    def test_a_enters_all_frames(self, window):
        _press(window, Qt.Key_S, "s")
        _press(window, Qt.Key_A, "a")
        assert window.mode() == "all"

    def test_m_no_longer_switches_mode(self, window):
        _press(window, Qt.Key_S, "s")
        _press(window, Qt.Key_M, "m")
        assert window.mode() == "single"

    def test_w_no_longer_switches_mode(self, window):
        _press(window, Qt.Key_A, "a")
        _press(window, Qt.Key_W, "w")
        assert window.mode() == "all"

    def test_the_inspector_exposes_both_modes_as_buttons(self, window):
        labels = {mode: (button.text(), button.shortcut_text())
                  for mode, button in window._mode_btns.items()}
        assert labels == {"single": ("Single Frame", "S"),
                          "all": ("All Frames", "A")}
        assert all(button.isVisibleTo(window._inspector)
                   for button in window._mode_btns.values())

    def test_the_two_modes_share_one_row(self, window):
        single = window._mode_btns["single"]
        assert single.parentWidget() is window._mode_btns["all"].parentWidget()
        assert single.parentWidget() is not window._toggle_btns["palette"].parentWidget()

    def test_the_buttons_and_the_keys_drive_the_same_transition(self, window):
        window._mode_btns["single"].click()
        by_button = window.mode()
        _press(window, Qt.Key_A, "a")
        _press(window, Qt.Key_S, "s")
        assert by_button == window.mode() == "single"

    def test_the_current_mode_is_shown_as_a_checked_button(self, window):
        window.set_mode("all")
        assert window._mode_btns["all"].isChecked() is True
        assert window._mode_btns["single"].isChecked() is False
        _press(window, Qt.Key_S, "s")
        assert window._mode_btns["single"].isChecked() is True
        assert window._mode_btns["all"].isChecked() is False

    def test_the_selected_frame_survives_mode_changes(self, window):
        window.set_mode("all")
        window._select_frame(2)
        window.set_mode("single")
        assert window._current == 2
        window.set_mode("all")
        assert window._current == 2
        assert window.current_frame()["shot_id"] == "first.mkv@2"

    def test_the_scrubber_selects_a_frame(self, window):
        window._browser.frame_selected.emit(2)
        assert window._current == 2

    def test_the_scrubber_is_the_shared_shotlist_widget(self, window):
        from visualizers.components.timeline_scrubber import (
            TimelineHitArea, TimelineScrollBar,
        )

        assert isinstance(window._browser._scrub, TimelineHitArea)
        assert isinstance(window._browser._scrub_bar, TimelineScrollBar)

    def test_up_and_down_step_a_grid_row_in_all_frames(self, window):
        window.set_mode("all")
        window._select_frame(0)
        columns = window._browser.columns()
        _press(window, Qt.Key_Down)
        assert window._current == min(columns, len(window._frames) - 1)
        _press(window, Qt.Key_Up)
        assert window._current == 0

    def test_up_and_down_do_nothing_in_single_frame(self, window):
        window.set_mode("single")
        window._select_frame(1)
        _press(window, Qt.Key_Down)
        assert window._current == 1
        _press(window, Qt.Key_Up)
        assert window._current == 1

    def test_left_and_right_still_work_in_single_frame(self, window):
        window.set_mode("single")
        window._select_frame(1)
        _press(window, Qt.Key_Right)
        assert window._current == 2
        _press(window, Qt.Key_Left)
        assert window._current == 1

    def test_row_stepping_is_clamped_to_the_frame_list(self, window):
        window.set_mode("all")
        window._select_frame(0)
        window.step_row(-1)
        assert window._current == 0
        for _ in range(20):
            window.step_row(1)
        assert 0 <= window._current < len(window._frames)

    def test_selecting_a_frame_scrolls_it_into_view(self, window):
        window.set_mode("all")
        page = window._browser
        page.resize(300, 200)
        page.layout().activate()
        calls = []
        page._scroll.ensureWidgetVisible = lambda widget, *a, **k: calls.append(widget)
        window._select_frame(len(window._frames) - 1)
        assert calls and calls[-1] is page._cells[len(window._frames) - 1]

    def test_double_click_in_all_frames_opens_that_frame(self, window):
        window.set_mode("all")
        window._browser.frame_activated.emit(2)
        assert window._current == 2
        assert window.mode() == "single"

    def test_double_click_in_single_frame_returns_to_all_frames(self, window):
        window.set_mode("single")
        window._select_frame(2)
        window._browser.canvas().double_clicked.emit()
        assert window.mode() == "all"
        assert window._current == 2

    def test_a_double_click_while_armed_does_not_change_mode(self, window):
        window.set_mode("single")
        window.assign_role(store.ROLE_FIGURE)
        canvas = window._browser.canvas()
        emitted = []
        canvas.double_clicked.connect(lambda: emitted.append(True))
        canvas.mouseDoubleClickEvent(_double_click())
        assert emitted == []
        assert window.mode() == "single"

    def test_the_scrub_area_does_not_leave_a_tall_empty_band(self, window):
        from styles import theme
        from visualizers.palette_visualizer import SCRUB_BARS

        scrub = window._browser._scrub
        assert scrub.height() == theme.SCROLLBAR_W * SCRUB_BARS
        assert SCRUB_BARS < 15

    def test_the_scrub_area_floats_over_the_frames(self, window):
        page = window._browser
        scrub = page._scrub
        # Parented to the page, not added to its layout, so it overlays.
        assert scrub.parentWidget() is page
        assert page.layout().indexOf(scrub) == -1
        assert "transparent" in scrub.styleSheet()
        assert scrub.testAttribute(Qt.WA_StyledBackground) is True

    def test_the_frames_reach_the_bottom_of_the_browser(self, window):
        page = window._browser
        page.resize(800, 600)
        page.layout().activate()
        assert page._stack.height() == page.height()
        assert page._stack.geometry().bottom() == page.rect().bottom()

    def test_the_scrubber_sits_on_the_bottom_edge(self, window):
        from PyQt5.QtCore import QSize
        from PyQt5.QtGui import QResizeEvent

        page = window._browser
        page.resize(800, 600)
        page.resizeEvent(QResizeEvent(QSize(800, 600), QSize(0, 0)))
        assert page._scrub.geometry().bottom() == page.rect().bottom()
        assert page._scrub.width() == page.width()

    def test_the_floating_grab_band_stays_tight(self, window):
        from styles import theme
        from visualizers.palette_visualizer import SCRUB_BARS

        # Every pixel of the band is a pixel of grid that cannot be clicked.
        assert SCRUB_BARS == 2
        assert window._browser._scrub.height() == theme.SCROLLBAR_W * SCRUB_BARS


class TestKeyboardModel:
    def test_g_creates_proposals_and_never_changes_display_mode(self, window, monkeypatch):
        called = []
        monkeypatch.setattr(window, "create_palette", lambda: called.append(True))
        before = window.mode()
        _press(window, Qt.Key_G, "g")
        assert called == [True]
        assert window.mode() == before

    def test_c_is_no_longer_bound(self, window, monkeypatch):
        called = []
        monkeypatch.setattr(window, "create_palette", lambda: called.append(True))
        _press(window, Qt.Key_C, "c")
        assert called == []

    def test_g_is_not_bound_to_any_display_action(self, window):
        before = (window.mode(), window._show_image, window._show_palette)
        window._worker = object()          # suppress real generation
        _press(window, Qt.Key_G, "g")
        window._worker = None
        assert (window.mode(), window._show_image, window._show_palette) == before

    def test_there_is_no_create_all_keyboard_action(self, window, monkeypatch):
        called = []
        monkeypatch.setattr(window, "create_all_palettes", lambda: called.append(True))
        for code in range(Qt.Key_A, Qt.Key_Z + 1):
            window._worker = object()
            _press(window, code, chr(code).lower())
            window._worker = None
        assert called == []

    def test_clear_all_has_no_keyboard_action(self, window, monkeypatch):
        called = []
        monkeypatch.setattr(window, "clear_all", lambda: called.append(True))
        for code in range(Qt.Key_A, Qt.Key_Z + 1):
            _press(window, code, chr(code).lower())
        assert called == []

    def test_p_toggles_palette_independently_of_image(self, window):
        _press(window, Qt.Key_P, "p")
        assert window._show_palette is False
        assert window._show_image is True

    def test_i_toggles_image_independently_of_palette(self, window):
        _press(window, Qt.Key_I, "i")
        assert window._show_image is False
        assert window._show_palette is True

    def test_toggling_visibility_does_not_change_the_frame(self, window):
        _press(window, Qt.Key_Right)
        before = window._current
        _press(window, Qt.Key_P, "p")
        _press(window, Qt.Key_I, "i")
        assert window._current == before

    def test_shortcuts_do_not_fire_while_editing_text(self, window, app):
        editor = QLineEdit()
        editor.show()
        editor.setFocus()
        app.processEvents()
        if not window._editing_text():
            pytest.skip("focus not grantable on this platform")
        before = window._current
        _press(window, Qt.Key_Right)
        _press(window, Qt.Key_M, "m")
        assert window._current == before
        editor.close()

    def test_zoom_keys_still_work(self, window):
        manager = window._browser.zoom_manager()
        start = manager.zoom()
        _press(window, Qt.Key_Plus, "+", Qt.ControlModifier)
        zoomed = manager.zoom()
        _press(window, Qt.Key_Minus, "-", Qt.ControlModifier)
        assert zoomed > start
        assert manager.zoom() < zoomed


class TestProposalReview:
    def test_a_valid_choice_accepts_without_leaving_the_frame(self, window):
        _generated(window)
        before = window._current
        assert window.accept_choice("1") is True
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type)["frames"]["first.mkv@0"]
        assert entry["review"]["choice"] == "1"
        assert window._current == before

    def test_a_missing_choice_does_nothing(self, window):
        _generated(window, choices=("1",))
        before = window._current
        assert window.accept_choice("3") is False
        assert window._current == before

    def test_an_ungenerated_frame_accepts_nothing(self, window):
        before = window._current
        assert window.accept_choice("1") is False
        assert window._current == before

    def test_a_choice_without_two_colours_is_not_selectable(self, window):
        _generated(window)
        entry = window._entry()
        entry["proposals"]["2"]["colours"] = []
        assert "2" not in store.selectable_choices(entry)

    def test_pressing_a_refusal_choice_does_not_raise(self, window):
        """The refusal quadrant is active but has no palette — it must be inert."""
        _generated(window)
        record = store.load_review(window._project_path, window._filename,
                                   window._media_type)
        entry = record["frames"]["first.mkv@0"]
        entry["proposals"]["3"] = {
            "strategy": "no_adequate_two_colour",
            "label": "NO ADEQUATE TWO-COLOUR PALETTE",
            "active": True, "materials": [], "converges_with": [], "colours": [],
        }
        store.save_review(window._project_path, window._filename,
                          window._media_type, record)
        window.reload()
        window._select_frame(0)

        assert window._choice_btns["3"].isEnabled() is False
        assert window.accept_choice("3") is False
        _press(window, Qt.Key_3, "3")
        reloaded = store.load_review(window._project_path, window._filename,
                                     window._media_type)["frames"]["first.mkv@0"]
        assert "review" not in reloaded

    def test_unavailable_choices_are_greyed_out(self, window):
        _generated(window, choices=("1",))
        assert window._choice_btns["1"].isEnabled() is True
        assert window._choice_btns["3"].isEnabled() is False

    def test_the_keyboard_accepts_the_same_way_as_the_button(self, window):
        _generated(window)
        _press(window, Qt.Key_2, "2")
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type)["frames"]["first.mkv@0"]
        assert entry["review"]["choice"] == "2"

    def test_rejection_is_persisted_without_leaving_the_frame(self, window):
        _generated(window)
        before = window._current
        window.reject_proposals()
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type)["frames"]["first.mkv@0"]
        assert entry["review"]["answer"] == store.ANSWER_REJECTED
        assert window._current == before

    def test_only_the_arrow_keys_change_frame(self, window):
        _generated(window)
        before = window._current
        _press(window, Qt.Key_1, "1")
        _press(window, Qt.Key_2, "2")
        assert window._current == before
        _press(window, Qt.Key_Right)
        assert window._current == before + 1

    def test_reset_returns_the_frame_to_its_proposals(self, window):
        _generated(window)
        window.accept_choice("1")
        window._select_frame(0)
        _press(window, Qt.Key_Delete)
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type)["frames"]["first.mkv@0"]
        assert store.frame_state(entry) == store.STATE_UNREVIEWED
        assert entry["proposals"]

    def test_reset_does_not_advance(self, window):
        _generated(window)
        window.accept_choice("1")
        window._select_frame(0)
        _press(window, Qt.Key_Backspace)
        assert window._current == 0


class TestManualAuthoring:
    def test_a_role_button_arms_a_colour_pick(self, window):
        window.assign_role(store.ROLE_FIGURE)
        assert window._pick_role == store.ROLE_FIGURE
        assert window._role_btns[store.ROLE_FIGURE].isChecked() is True
        assert window._browser.canvas().pick_role() == store.ROLE_FIGURE

    def test_pressing_the_same_role_again_disarms(self, window):
        window.assign_role(store.ROLE_FIGURE)
        window.assign_role(store.ROLE_FIGURE)
        assert window._pick_role is None
        assert window._role_btns[store.ROLE_FIGURE].isChecked() is False
        assert window._browser.canvas().pick_role() is None

    def test_arming_one_role_disarms_the_other(self, window):
        window.assign_role(store.ROLE_FIGURE)
        window.assign_role(store.ROLE_BACKGROUND)
        assert window._pick_role == store.ROLE_BACKGROUND
        assert window._role_btns[store.ROLE_FIGURE].isChecked() is False
        assert window._role_btns[store.ROLE_BACKGROUND].isChecked() is True

    def test_the_f_and_b_keys_arm_the_same_way_as_the_buttons(self, window):
        _press(window, Qt.Key_F, "f")
        assert window._pick_role == store.ROLE_FIGURE
        _press(window, Qt.Key_B, "b")
        assert window._pick_role == store.ROLE_BACKGROUND

    def test_a_pick_assigns_the_clicked_colour_and_disarms(self, window):
        window.assign_role(store.ROLE_FIGURE)
        window._on_pipette(220, 30, 25, store.ROLE_FIGURE)
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type)["frames"]["first.mkv@0"]
        stored = entry["manual"][store.ROLE_FIGURE]
        assert stored["rgb"] == [220, 30, 25]
        assert stored["pipette"] is True
        assert window._pick_role is None

    def test_an_armed_canvas_click_needs_no_segmentation(self, window, monkeypatch):
        started = []
        monkeypatch.setattr(window, "_on_sam_requested",
                            lambda *a: started.append(True))
        canvas = window._browser.canvas()
        canvas.set_pick_role(store.ROLE_FIGURE)
        assert canvas.pick_role() == store.ROLE_FIGURE
        assert started == []

    def test_a_pipetted_figure_colour_persists_verbatim(self, window):
        window._on_pipette(220, 30, 25, store.ROLE_FIGURE)
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type)["frames"]["first.mkv@0"]
        stored = entry["manual"][store.ROLE_FIGURE]
        assert stored["rgb"] == [220, 30, 25]
        assert stored["pipette"] is True

    def test_a_pipetted_background_colour_persists(self, window):
        window._on_pipette(10, 11, 12, store.ROLE_BACKGROUND)
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type)["frames"]["first.mkv@0"]
        assert entry["manual"][store.ROLE_BACKGROUND]["hex"] == "#0a0b0c"

    def test_one_role_alone_is_an_incomplete_manual_palette(self, window):
        window._on_pipette(1, 2, 3, store.ROLE_FIGURE)
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type)["frames"]["first.mkv@0"]
        assert store.frame_state(entry) == store.STATE_MANUAL_INCOMPLETE

    def test_manual_editing_does_not_auto_advance(self, window):
        before = window._current
        window._on_pipette(1, 2, 3, store.ROLE_FIGURE)
        window._on_pipette(4, 5, 6, store.ROLE_BACKGROUND)
        assert window._current == before

    def test_segmentation_reports_no_status_text_on_success(self, window):
        window._on_masks_ready([])
        assert window._manual_lbl.isVisible() is False
        assert window._manual_lbl.text() == ""

    def test_segmentation_failure_stays_visible(self, window):
        window._on_masks_failed("no model")
        assert "no model" in window._manual_lbl.text()

    def test_the_manual_section_has_its_own_sweep_bar(self, window):
        from visualizers.components.sweep_bar import SweepBar

        assert isinstance(window._manual_sweep, SweepBar)
        assert window._manual_sweep is not window._sweep

    def test_masks_are_never_unioned_automatically(self, window):
        from visualizers.palette_visualizer import build_blobs
        import numpy as np

        masks = [{"segmentation": np.ones((6, 6), dtype=bool)},
                 {"segmentation": np.zeros((6, 6), dtype=bool)}]
        masks[1]["segmentation"][0:3, 0:3] = True
        blobs = build_blobs(masks)
        assert len(blobs) == 2
        assert window._browser.canvas().selected_indices() == []


class TestDisplayToggles:
    def test_the_inspector_exposes_palette_and_image_toggles(self, window):
        labels = {name: (button.text(), button.shortcut_text())
                  for name, button in window._toggle_btns.items()}
        assert labels == {"palette": ("Palette", "P"), "image": ("Image", "I")}

    def test_the_two_toggles_share_one_row(self, window):
        assert (window._toggle_btns["palette"].parentWidget()
                is window._toggle_btns["image"].parentWidget())

    def test_both_start_on_and_checked(self, window):
        assert (window._show_palette, window._show_image) == (True, True)
        assert all(button.isChecked() for button in window._toggle_btns.values())

    def test_the_buttons_and_the_keys_drive_the_same_state(self, window):
        window._toggle_btns["palette"].click()
        by_button = window._show_palette
        _press(window, Qt.Key_P, "p")
        _press(window, Qt.Key_P, "p")
        assert by_button is False
        assert window._show_palette is False

    def test_the_checked_state_follows_the_keyboard(self, window):
        _press(window, Qt.Key_P, "p")
        assert window._toggle_btns["palette"].isChecked() is False
        assert window._toggle_btns["image"].isChecked() is True
        _press(window, Qt.Key_I, "i")
        assert window._toggle_btns["image"].isChecked() is False
        _press(window, Qt.Key_P, "p")
        assert window._toggle_btns["palette"].isChecked() is True

    def test_the_toggles_are_independent(self, window):
        _press(window, Qt.Key_I, "i")
        assert (window._show_palette, window._show_image) == (True, False)


class TestProposalPreview:
    def test_an_ungenerated_frame_previews_nothing(self, window):
        from visualizers.palette_visualizer import preview_quadrants

        assert preview_quadrants({}) == [None, None, None, None]

    def test_selectable_proposals_fill_their_own_quadrant(self, window):
        from visualizers.palette_visualizer import preview_quadrants

        _generated(window, choices=("1", "3"))
        quadrants = preview_quadrants(window._entry())
        assert [q["key"] if q else None for q in quadrants] == ["1", None, "3", None]
        assert quadrants[0]["figure"]["hex"] == "#010203"
        assert quadrants[0]["background"]["hex"] == "#010506"

    def test_an_unselectable_choice_leaves_its_quadrant_empty(self, window):
        from visualizers.palette_visualizer import preview_quadrants

        _generated(window)
        entry = window._entry()
        entry["proposals"]["2"]["active"] = False
        assert preview_quadrants(entry)[1] is None

    def test_a_choice_without_two_colours_leaves_its_quadrant_empty(self, window):
        from visualizers.palette_visualizer import preview_quadrants

        _generated(window)
        entry = window._entry()
        entry["proposals"]["2"]["colours"] = []
        assert preview_quadrants(entry)[1] is None

    def test_the_preview_reaches_the_browser_and_the_canvas(self, window):
        _generated(window)
        item = window.current_frame()
        assert any(item["preview"])
        assert any(window._browser.canvas()._preview)

    def test_accepting_replaces_the_preview_with_the_chosen_palette(self, window):
        _generated(window)
        window.accept_choice("1")
        window._select_frame(0)
        item = window.current_frame()
        assert item["final"]["source"] == "proposal"
        assert item["final"][store.ROLE_FIGURE]["hex"] == "#010203"

    def test_resetting_brings_the_preview_back(self, window):
        _generated(window)
        window.accept_choice("1")
        window._select_frame(0)
        window.reset_frame()
        item = window.current_frame()
        assert item["final"] == {}
        assert any(item["preview"])

    def test_the_frame_layout_is_two_by_two(self):
        from PyQt5.QtCore import QRect

        from visualizers.palette_visualizer import preview_cells

        cells = preview_cells(QRect(0, 0, 120, 68), 4, columns=2)
        assert len(cells) == 4
        assert [(c.left(), c.top()) for c in cells] == [(0, 0), (60, 0), (0, 34), (60, 34)]
        assert sum(c.width() * c.height() for c in cells) == 120 * 68

    def test_the_strip_layout_is_one_by_four(self):
        from PyQt5.QtCore import QRect

        from visualizers.palette_visualizer import preview_cells

        cells = preview_cells(QRect(0, 0, 400, 36), 4, columns=4)
        assert [c.top() for c in cells] == [0, 0, 0, 0]
        assert [c.left() for c in cells] == [0, 100, 200, 300]
        assert all(c.height() == 36 for c in cells)

    def test_the_layout_tiles_an_offset_rect_without_gaps(self):
        from PyQt5.QtCore import QRect

        from visualizers.palette_visualizer import preview_cells

        rect = QRect(11, 7, 101, 45)
        for columns in (2, 4):
            cells = preview_cells(rect, 4, columns=columns)
            assert min(c.left() for c in cells) == rect.left()
            assert max(c.left() + c.width() for c in cells) == rect.left() + rect.width()
            assert sum(c.width() * c.height() for c in cells) == rect.width() * rect.height()


class TestGenerateProgress:
    def test_progress_is_reported_in_the_section_title(self, window):
        window._on_progress(3, 9, "shot", "generated")
        assert window._generate_section._header.text() == "Generate: 3 / 9"

    def test_there_is_no_progress_text_row(self, window):
        from PyQt5.QtWidgets import QLabel

        assert not hasattr(window, "_progress_lbl")
        container = window._create_btn.parentWidget().parentWidget()
        for label in container.findChildren(QLabel):
            assert "generated" not in label.text().lower()

    def test_batch_failures_are_printed_not_shown(self, window, capsys):
        window._on_generation_done(
            {"generated": 1, "skipped": 0, "failed": 1,
             "errors": [("shot-7", "SAM exploded")]})
        captured = capsys.readouterr()
        assert "shot-7" in captured.err
        assert "SAM exploded" in captured.err

    def test_a_whole_run_failure_is_printed(self, window, capsys):
        window._on_generation_failed("RuntimeError: no model")
        assert "no model" in capsys.readouterr().err

    def test_the_title_returns_to_plain_when_generation_stops(self, window):
        window._on_progress(1, 1, "shot", "generated")
        window._stop_worker()
        assert window._generate_section._header.text() == "Generate"


class TestStagedReset:
    def test_an_ungenerated_frame_offers_nothing_to_undo(self, window):
        from visualizers.palette_visualizer import reset_action

        assert reset_action({}) == ("Reset", False)
        window._select_frame(0)
        assert window._reset_btn.text() == "Reset"
        assert window._reset_btn.isEnabled() is False

    def test_proposals_without_a_choice_offer_reset_proposals(self, window):
        from visualizers.palette_visualizer import reset_action

        _generated(window)
        window._select_frame(0)
        assert reset_action(window._entry()) == ("Reset Proposals", True)
        assert window._reset_btn.text() == "Reset Proposals"
        assert window._reset_btn.isEnabled() is True

    def test_a_chosen_palette_offers_reset_palette(self, window):
        from visualizers.palette_visualizer import reset_action

        _generated(window)
        window.accept_choice("1")
        window._select_frame(0)
        assert reset_action(window._entry()) == ("Reset Palette", True)
        assert window._reset_btn.text() == "Reset Palette"

    def test_an_incomplete_manual_palette_offers_reset_palette(self, window):
        from visualizers.palette_visualizer import reset_action

        window._on_pipette(1, 2, 3, store.ROLE_FIGURE)
        assert reset_action(window._entry())[0] == "Reset Palette"

    def test_a_failed_generation_offers_reset_proposals(self, window):
        from visualizers.palette_visualizer import reset_action

        entry = store.record_generation_failure({}, "v", "g", "boom")
        assert reset_action(entry) == ("Reset Proposals", True)

    def test_reset_steps_a_chosen_palette_back_to_its_proposals(self, window):
        _generated(window)
        window.accept_choice("1")
        window._select_frame(0)
        _press(window, Qt.Key_Delete)
        entry = window._entry()
        assert store.frame_state(entry) == store.STATE_UNREVIEWED
        assert entry["proposals"]

    def test_reset_again_discards_the_proposals(self, window):
        _generated(window)
        window.accept_choice("1")
        window._select_frame(0)
        _press(window, Qt.Key_Delete)
        _press(window, Qt.Key_Delete)
        entry = window._entry()
        assert store.frame_state(entry) == store.STATE_NOT_GENERATED
        assert "proposals" not in entry
        assert "proposal_generation" not in entry

    def test_a_third_reset_does_nothing(self, window):
        _generated(window)
        for _ in range(3):
            window._select_frame(0)
            _press(window, Qt.Key_Delete)
        assert store.frame_state(window._entry()) == store.STATE_NOT_GENERATED

    def test_the_store_keeps_reset_and_clear_distinct(self):
        kept = store.reset_frame(store.accept_proposal(_generated_entry(), "1"))
        assert kept["proposals"] and kept["proposal_generation"]
        cleared = store.clear_proposals(store.accept_proposal(_generated_entry(), "1"))
        assert "proposals" not in cleared
        assert "proposal_generation" not in cleared
        assert "review" not in cleared


class TestPartialManualDisplay:
    def test_a_complete_palette_is_displayed(self, window):
        from visualizers.palette_visualizer import display_palette

        _generated(window)
        window.accept_choice("1")
        window._select_frame(0)
        shown = display_palette(window._entry())
        assert shown[store.ROLE_FIGURE]["hex"] == "#010203"
        assert shown[store.ROLE_BACKGROUND]["hex"] == "#010506"

    def test_a_figure_only_manual_palette_still_shows_the_figure(self, window):
        from visualizers.palette_visualizer import display_palette

        window._on_pipette(220, 30, 25, store.ROLE_FIGURE)
        shown = display_palette(window._entry())
        assert shown[store.ROLE_FIGURE]["rgb"] == [220, 30, 25]
        assert store.ROLE_BACKGROUND not in shown

    def test_a_background_only_manual_palette_still_shows_the_background(self, window):
        from visualizers.palette_visualizer import display_palette

        window._on_pipette(8, 9, 10, store.ROLE_BACKGROUND)
        shown = display_palette(window._entry())
        assert shown[store.ROLE_BACKGROUND]["rgb"] == [8, 9, 10]
        assert store.ROLE_FIGURE not in shown

    def test_an_empty_frame_displays_nothing(self):
        from visualizers.palette_visualizer import display_palette

        assert display_palette({}) == {}

    def test_the_partial_palette_reaches_the_canvas(self, window):
        window.set_mode("single")
        window._on_pipette(220, 30, 25, store.ROLE_FIGURE)
        assert window._browser.canvas()._palette[store.ROLE_FIGURE]["rgb"] == [220, 30, 25]

    def test_the_centre_disc_is_twice_the_border_thickness(self):
        from visualizers.palette_visualizer import (
            SINGLE_BORDER_RATIO, SINGLE_FIGURE_RATIO,
        )

        assert SINGLE_BORDER_RATIO == 0.10
        assert SINGLE_FIGURE_RATIO == 0.20


class TestGenerationControls:
    class _FakeWorker:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def isRunning(self):  # noqa: N802
            return False

    def _running(self, window, button):
        worker = self._FakeWorker()
        window._worker = worker
        window._active_btn = button
        other = (window._create_all_btn if button is window._create_btn
                 else window._create_btn)
        button.setText("Cancel Proposals")
        other.setEnabled(False)
        return worker

    def test_the_running_button_offers_cancel(self, window):
        worker = self._running(window, window._create_btn)
        assert window._create_btn.text() == "Cancel Proposals"
        assert window._create_all_btn.isEnabled() is False
        window.create_palette()
        assert worker.cancelled is True

    def test_the_batch_button_offers_cancel_too(self, window):
        worker = self._running(window, window._create_all_btn)
        window.create_all_palettes()
        assert worker.cancelled is True
        assert window._create_btn.isEnabled() is False

    def test_the_idle_button_does_not_start_a_second_run(self, window):
        worker = self._running(window, window._create_all_btn)
        window.create_palette()
        assert worker.cancelled is False
        assert window._worker is worker

    def test_stopping_restores_both_labels(self, window):
        self._running(window, window._create_btn)
        window._stop_worker()
        assert window._create_btn.text() == "Generate Proposals"
        assert window._create_all_btn.text() == "Generate All Remaining"
        assert window._active_btn is None

    def test_cancelling_keeps_finished_work(self, window):
        _generated(window)
        worker = self._running(window, window._create_all_btn)
        window.cancel_generation()
        window._stop_worker()
        assert worker.cancelled is True
        assert window._entry()["proposals"]

    def test_completed_frames_become_reviewable_during_a_run(self, window):
        """A frame that lands mid-batch is selectable without waiting."""
        self._running(window, window._create_all_btn)
        _generated(window)
        shot = window.current_frame()["shot_id"]
        window._on_progress(1, 4, shot, "generated")
        assert store.selectable_choices(window._entry()) == ["1", "2"]
        assert window._choice_btns["1"].isEnabled() is True
        window._worker = None
        window._active_btn = None

    def test_progress_updates_one_cell_not_the_whole_grid(self, window, monkeypatch):
        _generated(window)
        rebuilds = []
        monkeypatch.setattr(window._browser, "set_frames",
                            lambda frames: rebuilds.append(frames))
        window._refresh_frame(window.current_frame()["shot_id"])
        assert rebuilds == []

    def test_both_buttons_grey_out_when_nothing_remains(self, window):
        for item in window._frames:
            item["state"] = store.STATE_ACCEPTED
        window._refresh_inspector()
        assert window._create_btn.isEnabled() is False
        assert window._create_all_btn.isEnabled() is False

    def test_the_batch_button_stays_live_while_any_frame_remains(self, window):
        for item in window._frames:
            item["state"] = store.STATE_ACCEPTED
        window._frames[-1]["state"] = store.STATE_NOT_GENERATED
        window._refresh_inspector()
        assert window._create_all_btn.isEnabled() is True

    def test_a_manual_palette_counts_as_done(self, window):
        from visualizers.palette_visualizer import needs_generation

        assert needs_generation({"state": store.STATE_MANUAL}) is False
        assert needs_generation({"state": store.STATE_MANUAL_INCOMPLETE}) is False
        assert needs_generation({"state": store.STATE_NOT_GENERATED}) is True
        assert needs_generation({"state": store.STATE_GENERATION_FAILED}) is True


class TestPipettePreview:
    def _canvas(self, window, tmp_path):
        from PyQt5.QtGui import QImage

        image = QImage(40, 20, QImage.Format_RGB32)
        image.fill(QColor(200, 30, 40))
        path = tmp_path / "solid.png"
        image.save(str(path))
        canvas = window._browser.canvas()
        canvas.set_frame(str(path))
        return canvas

    def _move(self, canvas):
        from PyQt5.QtGui import QMouseEvent

        # The canvas enforces a 320x240 minimum, so aim at its real centre.
        canvas.mouseMoveEvent(QMouseEvent(
            QEvent.MouseMove, canvas.rect().center(), Qt.NoButton, Qt.NoButton,
            Qt.NoModifier))

    def test_moving_while_armed_previews_the_colour(self, window, tmp_path):
        canvas = self._canvas(window, tmp_path)
        canvas.set_pick_role(store.ROLE_FIGURE)
        self._move(canvas)
        assert canvas.pick_preview()["rgb"] == [200, 30, 40]
        assert canvas.effective_palette()[store.ROLE_FIGURE]["rgb"] == [200, 30, 40]

    def test_the_preview_only_touches_the_armed_role(self, window, tmp_path):
        canvas = self._canvas(window, tmp_path)
        canvas.set_palette({store.ROLE_BACKGROUND: {"rgb": [1, 1, 1]}})
        canvas.set_pick_role(store.ROLE_FIGURE)
        self._move(canvas)
        shown = canvas.effective_palette()
        assert shown[store.ROLE_FIGURE]["rgb"] == [200, 30, 40]
        assert shown[store.ROLE_BACKGROUND]["rgb"] == [1, 1, 1]

    def test_moving_unarmed_previews_nothing(self, window, tmp_path):
        canvas = self._canvas(window, tmp_path)
        canvas.set_pick_role(None)
        self._move(canvas)
        assert canvas.pick_preview() is None

    def test_disarming_reverts_to_the_previous_state(self, window, tmp_path):
        canvas = self._canvas(window, tmp_path)
        canvas.set_palette({store.ROLE_FIGURE: {"rgb": [9, 9, 9]}})
        canvas.set_pick_role(store.ROLE_FIGURE)
        self._move(canvas)
        assert canvas.effective_palette()[store.ROLE_FIGURE]["rgb"] == [200, 30, 40]
        canvas.set_pick_role(None)
        assert canvas.pick_preview() is None
        assert canvas.effective_palette()[store.ROLE_FIGURE]["rgb"] == [9, 9, 9]

    def test_disarming_an_undefined_role_leaves_it_undefined(self, window, tmp_path):
        canvas = self._canvas(window, tmp_path)
        canvas.set_palette({})
        canvas.set_pick_role(store.ROLE_BACKGROUND)
        self._move(canvas)
        assert store.ROLE_BACKGROUND in canvas.effective_palette()
        canvas.set_pick_role(None)
        assert canvas.effective_palette() == {}

    def test_pressing_the_role_key_again_cancels_the_preview(self, window, tmp_path):
        canvas = self._canvas(window, tmp_path)
        window.assign_role(store.ROLE_FIGURE)
        self._move(canvas)
        assert canvas.pick_preview() is not None
        window.assign_role(store.ROLE_FIGURE)
        assert canvas.pick_role() is None
        assert canvas.pick_preview() is None

    def test_leaving_the_canvas_clears_the_preview(self, window, tmp_path):
        canvas = self._canvas(window, tmp_path)
        canvas.set_pick_role(store.ROLE_FIGURE)
        self._move(canvas)
        canvas.leaveEvent(QEvent(QEvent.Leave))
        assert canvas.pick_preview() is None


class TestExperimentingOnOneFrame:
    """Try proposals, pipette, and go back — all without leaving the frame."""

    def _entry(self, window):
        return store.load_review(window._project_path, window._filename,
                                 window._media_type)["frames"]["first.mkv@0"]

    def test_switching_between_proposals_replaces_the_palette(self, window):
        _generated(window)
        before = window._current
        window.accept_choice("1")
        window.accept_choice("2")
        entry = self._entry(window)
        assert entry["review"]["choice"] == "2"
        assert store.frame_state(entry) == store.STATE_ACCEPTED
        assert window._current == before

    def test_pipetting_after_accepting_replaces_only_that_half(self, window):
        from visualizers.palette_visualizer import display_palette

        _generated(window)
        window.accept_choice("1")
        window._on_pipette(220, 30, 25, store.ROLE_FIGURE)
        entry = self._entry(window)
        shown = display_palette(entry)
        assert shown[store.ROLE_FIGURE]["rgb"] == [220, 30, 25]
        # The half nobody touched keeps coming from the proposal.
        assert shown[store.ROLE_BACKGROUND]["hex"] == "#010506"
        assert store.frame_state(entry) == store.STATE_SPLIT
        assert entry["review"]["roles"][store.ROLE_BACKGROUND]["choice"] == "1"
        assert store.ROLE_FIGURE not in entry["review"]["roles"]

    def test_a_half_pipetted_frame_shows_the_pick_beside_the_kept_half(self, window):
        from visualizers.palette_visualizer import display_palette

        _generated(window)
        window.accept_choice("1")
        window._on_pipette(9, 9, 9, store.ROLE_BACKGROUND)
        shown = display_palette(self._entry(window))
        assert shown[store.ROLE_BACKGROUND]["rgb"] == [9, 9, 9]
        assert shown[store.ROLE_FIGURE]["hex"] == "#010203"

    def test_pipetting_both_halves_leaves_a_wholly_manual_palette(self, window):
        _generated(window)
        window.accept_choice("1")
        window._on_pipette(220, 30, 25, store.ROLE_FIGURE)
        window._on_pipette(9, 9, 9, store.ROLE_BACKGROUND)
        entry = self._entry(window)
        assert store.frame_state(entry) == store.STATE_MANUAL
        assert "review" not in entry

    def test_going_back_to_a_proposal_after_pipetting(self, window):
        from visualizers.palette_visualizer import display_palette

        _generated(window)
        window._on_pipette(220, 30, 25, store.ROLE_FIGURE)
        window._on_pipette(1, 2, 3, store.ROLE_BACKGROUND)
        assert store.frame_state(self._entry(window)) == store.STATE_MANUAL

        assert window.accept_choice("2") is True
        entry = self._entry(window)
        assert "manual" not in entry
        assert store.frame_state(entry) == store.STATE_ACCEPTED
        assert display_palette(entry)[store.ROLE_FIGURE]["hex"] == "#020203"

    def test_the_choices_stay_selectable_while_hand_authoring(self, window):
        _generated(window)
        window._on_pipette(220, 30, 25, store.ROLE_FIGURE)
        window._select_frame(0)
        assert store.selectable_choices(window._entry()) == ["1", "2"]
        assert window._choice_btns["1"].isEnabled() is True

    def test_the_round_trip_never_loses_the_proposals(self, window):
        _generated(window)
        window.accept_choice("1")
        window._on_pipette(1, 2, 3, store.ROLE_FIGURE)
        window.accept_choice("2")
        window._on_pipette(4, 5, 6, store.ROLE_BACKGROUND)
        entry = self._entry(window)
        assert set(entry["proposals"]) == {"1", "2"}
        assert entry["proposal_generation"]["generation_id"] == "gen-test"

    def test_reset_still_returns_to_the_proposals(self, window):
        _generated(window)
        window._on_pipette(1, 2, 3, store.ROLE_FIGURE)
        window._select_frame(0)
        _press(window, Qt.Key_Delete)
        entry = window._entry()
        assert store.frame_state(entry) == store.STATE_UNREVIEWED
        assert entry["proposals"]


class TestSplitValidationInTheWindow:
    """F then 1, B then 4 — take each half from a different proposal."""

    def _entry(self, window):
        return store.load_review(window._project_path, window._filename,
                                 window._media_type)["frames"]["first.mkv@0"]

    def test_arming_a_role_makes_a_number_take_only_that_half(self, window):
        _generated(window)
        _press(window, Qt.Key_F, "f")
        _press(window, Qt.Key_1, "1")
        _press(window, Qt.Key_B, "b")
        _press(window, Qt.Key_2, "2")
        final = self._entry(window)["final_palette"]
        assert final["source"] == "split"
        assert final[store.ROLE_FIGURE]["hex"] == "#010203"
        assert final[store.ROLE_BACKGROUND]["hex"] == "#020506"

    def test_the_number_disarms_the_role(self, window):
        _generated(window)
        window.assign_role(store.ROLE_FIGURE)
        window.accept_choice("1")
        assert window._pick_role is None

    def test_an_unarmed_number_still_takes_the_whole_proposal(self, window):
        _generated(window)
        _press(window, Qt.Key_1, "1")
        entry = self._entry(window)
        assert entry["review"]["answer"] == store.ANSWER_ACCEPTED
        assert store.frame_state(entry) == store.STATE_ACCEPTED

    def test_the_split_names_the_strategy_behind_each_half(self, window):
        _generated(window)
        window.assign_role(store.ROLE_FIGURE)
        window.accept_choice("1")
        window.assign_role(store.ROLE_BACKGROUND)
        window.accept_choice("2")
        roles = self._entry(window)["review"]["roles"]
        assert roles[store.ROLE_FIGURE]["strategy"] == "strategy-1"
        assert roles[store.ROLE_BACKGROUND]["strategy"] == "strategy-2"

    def test_a_greyed_choice_is_still_inert_while_a_role_is_armed(self, window):
        _generated(window, choices=("1",))
        window.assign_role(store.ROLE_FIGURE)
        assert window.accept_choice("3") is False
        assert "review" not in self._entry(window)
        assert window._pick_role == store.ROLE_FIGURE

    def test_half_a_split_is_drawn_on_the_canvas(self, window):
        from visualizers.palette_visualizer import display_palette

        _generated(window)
        window.assign_role(store.ROLE_BACKGROUND)
        window.accept_choice("2")
        shown = display_palette(self._entry(window))
        assert shown[store.ROLE_BACKGROUND]["hex"] == "#020506"
        assert store.ROLE_FIGURE not in shown

    def test_the_info_block_names_each_half_s_proposal(self, window):
        _generated(window)
        window.assign_role(store.ROLE_FIGURE)
        window.accept_choice("1")
        window.assign_role(store.ROLE_BACKGROUND)
        window.accept_choice("2")
        window._select_frame(0)
        shown = {key: label.text().replace("\u200b", "")
                 for key, label in window._info.labels().items()}
        assert shown["figure"].endswith("1")
        assert shown["background"].endswith("2")
        assert shown["state"] == store.STATE_SPLIT

    def test_a_split_frame_can_still_be_reset(self, window):
        _generated(window)
        window.assign_role(store.ROLE_FIGURE)
        window.accept_choice("1")
        window._select_frame(0)
        _press(window, Qt.Key_Delete)
        entry = window._entry()
        assert store.frame_state(entry) == store.STATE_UNREVIEWED
        assert entry["proposals"]

    def test_a_split_never_advances_either(self, window):
        _generated(window)
        before = window._current
        window.assign_role(store.ROLE_FIGURE)
        window.accept_choice("1")
        assert window._current == before


class TestPreviewSections:
    """Palette and Image previews of the current frame, in the Inspector."""

    def _section(self, window, title):
        from visualizers.components.collapsible_section import CollapsibleSection

        for section in window._inspector.findChildren(CollapsibleSection):
            if section._title == title:
                return section
        raise AssertionError(f"no {title!r} section")

    def test_both_previews_exist(self, window):
        assert set(window._previews) == {"palette", "image"}

    def test_they_start_folded_closed(self, window):
        for title in ("Palette", "Image"):
            assert self._section(window, title).is_expanded() is False

    def test_each_shows_one_of_the_two_thumbnail_renderings(self, window):
        palette, image = window._previews["palette"], window._previews["image"]
        assert (palette._show_palette, palette._show_image) == (True, False)
        assert (image._show_palette, image._show_image) == (False, True)

    def test_they_paint_with_the_browser_cell_painter(self, window):
        from visualizers.palette_visualizer import _FrameCell, _PreviewCell

        # Subclassing is what keeps a preview from drifting from the thumbnail.
        assert issubclass(_PreviewCell, _FrameCell)
        assert "paintEvent" not in vars(_PreviewCell)

    def test_they_follow_the_selected_frame(self, window):
        _generated(window)
        window.accept_choice("1")
        window._select_frame(0)
        first = window._previews["palette"]._item["shot_id"]
        window._select_frame(2)
        assert window._previews["palette"]._item["shot_id"] != first
        assert window._previews["image"]._item["shot_id"] == "first.mkv@2"

    def test_changing_frame_drops_the_cached_frame_image(self, window, tmp_path):
        from PyQt5.QtGui import QPixmap

        preview = window._previews["image"]
        preview._pixmap = QPixmap()
        preview.set_item({"shot_id": "other", "image": "x.png"})
        assert preview._pixmap is None

    def test_a_preview_keeps_the_media_frame_ratio(self, window):
        preview = window._previews["palette"]
        preview.resize(300, preview.height())
        preview.set_aspect(2.0)
        assert preview.height() == 150

    def test_a_preview_refits_when_the_inspector_widens(self, window):
        from PyQt5.QtCore import QSize
        from PyQt5.QtGui import QResizeEvent

        preview = window._previews["image"]
        preview.set_aspect(2.0)
        preview.resize(400, preview.height())
        # Qt does not deliver resizeEvent to a hidden widget.
        preview.resizeEvent(QResizeEvent(preview.size(), QSize(1, 1)))
        assert preview.height() == 200

    def test_a_preview_is_inert(self, window):
        from PyQt5.QtCore import QPoint
        from PyQt5.QtGui import QMouseEvent
        from PyQt5.QtWidgets import QApplication

        preview = window._previews["image"]
        seen = []
        preview.clicked.connect(seen.append)
        preview.double_clicked.connect(seen.append)
        for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            QApplication.sendEvent(preview, QMouseEvent(
                kind, QPoint(2, 2), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        assert seen == []

    def test_the_previews_are_independent_of_the_view_toggles(self, window):
        window.toggle_image()
        window.toggle_palette()
        palette, image = window._previews["palette"], window._previews["image"]
        assert (palette._show_palette, palette._show_image) == (True, False)
        assert (image._show_palette, image._show_image) == (False, True)


class TestInspectorLayout:
    def test_generate_contains_only_intentional_controls(self, window):
        from visualizers.components.shortcut_button import ShortcutButton

        section = window._generate_section
        buttons = section.findChildren(ShortcutButton)
        assert [(b.text(), b.shortcut_text()) for b in buttons] == [
            ("Generate Proposals", "G"), ("Generate All Remaining", "")]
        assert all(button.text().strip() for button in buttons)

    def test_the_two_generate_controls_share_one_row(self, window):
        assert (window._create_btn.parentWidget()
                is window._create_all_btn.parentWidget())

    def test_manual_has_no_explanatory_prose_block(self, window):
        from PyQt5.QtWidgets import QLabel

        container = window._manual_lbl.parentWidget()
        for label in container.findChildren(QLabel):
            assert len(label.text()) < 60, label.text()
        assert window._manual_lbl.text() == ""
        assert window._manual_lbl.isVisible() is False

    def test_manual_contains_the_two_role_controls(self, window):
        buttons = window._role_btns
        assert [(b.text(), b.shortcut_text()) for b in buttons.values()] == [
            ("Figure", "F"), ("Background", "B")]

    def test_the_two_role_controls_share_one_row(self, window):
        figure = window._role_btns[store.ROLE_FIGURE]
        assert figure.parentWidget() is window._role_btns[store.ROLE_BACKGROUND].parentWidget()

    def test_clear_all_explains_itself_in_a_tooltip(self, window):
        tooltip = window._clear_btn.toolTip()
        assert "Generated proposals are kept" in tooltip
        assert "movie/gameplay" in tooltip

    def test_shortcut_hints_use_the_shared_token(self, window):
        from styles import theme

        assert theme.SHORTCUT_TEXT == theme.TEXT_DIM
        hint = window._create_btn._shortcut_label
        assert theme.SHORTCUT_TEXT in hint.styleSheet()

    def test_buttons_without_a_shortcut_have_no_hint(self, window):
        assert window._create_all_btn._shortcut_label is None
        assert window._clear_btn._shortcut_label is None

    def test_the_manual_instructions_live_in_tooltips(self, window):
        from visualizers.palette_visualizer import MANUAL_HELP

        container = window._manual_lbl.parentWidget()
        assert container.toolTip() == MANUAL_HELP
        for button in window._role_btns.values():
            assert button.toolTip() == MANUAL_HELP
        for phrase in ("arms that role", "press 1-4", "Alt+Click", "Ctrl+Click",
                       "Press the button again to cancel"):
            assert phrase.lower() in MANUAL_HELP.lower()

    def test_review_has_no_explanatory_prose_block(self, window):
        from PyQt5.QtWidgets import QLabel

        assert not hasattr(window, "_choice_lbl")
        container = window._reset_btn.parentWidget()
        for label in container.findChildren(QLabel):
            assert len(label.text()) < 40, label.text()

    def test_the_choice_meanings_live_in_tooltips(self, window):
        from visualizers.palette_visualizer import CHOICE_HELP

        for button in window._choice_btns.values():
            assert CHOICE_HELP in button.toolTip()
        for phrase in ("DIRECT", "FIELD", "ALTERNATIVE", "CONTROL"):
            assert phrase in CHOICE_HELP

    def test_the_reject_button_is_gone(self, window):
        assert not hasattr(window, "_reject_btn")
        from visualizers.components.shortcut_button import ShortcutButton

        labels = [b.text() for b in window._inspector.findChildren(ShortcutButton)]
        assert not any("Reject" in label for label in labels)

    def test_manual_status_is_transient_state_not_documentation(self, window):
        window._set_manual_status("4 region(s) found")
        assert window._manual_lbl.isVisibleTo(window._manual_lbl.parentWidget())
        window._set_manual_status("")
        assert window._manual_lbl.isVisible() is False


class TestThemeHighlightPair:
    def test_the_highlight_foreground_is_dark_enough_to_read_on_yellow(self):
        from PyQt5.QtGui import qGray

        from styles import theme

        background = qGray(QColor(theme.ACCENT).rgb())
        foreground = qGray(QColor(theme.ACCENT_TEXT).rgb())
        assert background > 180, "highlight background is still light yellow"
        assert foreground < 90, "highlight text must be dark on yellow"
        assert background - foreground > 120

    def test_the_tooltip_is_canvas_grey_with_muted_text(self):
        from PyQt5.QtGui import qGray

        from styles import theme

        assert theme.TOOLTIP_BG == theme.CANVAS_BG
        assert theme.TOOLTIP_TEXT == theme.TEXT_DIM
        background = qGray(QColor(theme.TOOLTIP_BG).rgb())
        foreground = qGray(QColor(theme.TOOLTIP_TEXT).rgb())
        assert background < 90, "tooltip background should be the dark canvas grey"
        assert foreground > background + 60, "tooltip text must read against it"

    def test_the_tooltip_is_not_the_accent_highlight(self):
        from styles import theme

        rule = theme.tooltip_stylesheet()
        assert theme.ACCENT not in rule
        assert theme.ACCENT_TEXT not in rule

    def test_the_app_stylesheet_uses_the_tooltip_pair(self):
        from styles import theme

        tooltip = theme._STYLESHEET.split("QToolTip")[1].split("}")[0]
        assert f"background-color: {theme.TOOLTIP_BG};" in tooltip
        assert f"color: {theme.TOOLTIP_TEXT};" in tooltip

    def test_action_buttons_carry_the_tooltip_rule_through_the_cascade(self):
        from styles import theme

        # An Inspector ancestor's bare "background: transparent;" matches
        # QToolTip too and renders it black, so the rule must travel with the
        # nearest styled ancestor.
        sheet = theme.action_button_stylesheet()
        assert "QToolTip" in sheet
        assert theme.TOOLTIP_BG in sheet
        assert theme.TOOLTIP_TEXT in sheet

    def test_palette_inspector_tooltip_owners_carry_the_rule(self, window):
        from styles import theme

        for widget in (window._manual_lbl.parentWidget(), window._manual_lbl):
            assert widget.toolTip()
            assert "QToolTip" in widget.styleSheet()
            assert theme.TOOLTIP_BG in widget.styleSheet()

    def test_qt_palette_roles_carry_the_same_pairs(self):
        """The pairs are expressed through Qt's own roles, not bespoke tokens.

        Asserted by reading apply_theme rather than calling it — re-applying
        the style to a live QApplication mid-suite aborts the interpreter.
        """
        import inspect

        from styles import theme

        source = inspect.getsource(theme.apply_theme)
        assert "QPalette.Highlight,       QColor(ACCENT)" in source
        assert "QPalette.HighlightedText, QColor(ACCENT_TEXT)" in source
        assert "QPalette.ToolTipBase,     QColor(TOOLTIP_BG)" in source
        assert "QPalette.ToolTipText,     QColor(TOOLTIP_TEXT)" in source


class TestBrowserGrid:
    def test_unselected_cells_have_no_border(self, window):
        from visualizers import palette_visualizer

        assert not hasattr(palette_visualizer, "STATE_COLOURS")

    def test_only_the_current_cell_is_marked_selected(self, window):
        window.set_mode("all")
        window._select_frame(2)
        flags = [cell._selected for cell in window._browser._cells]
        assert flags.count(True) == 1
        assert flags[2] is True

    def test_the_selection_border_matches_the_canonical_width(self):
        from visualizers.palette_visualizer import SELECTION_BORDER

        assert SELECTION_BORDER == 2

    def test_the_grid_uses_the_canonical_spacing(self):
        from styles import theme
        from visualizers.palette_visualizer import _GAP, _MARGIN

        assert _GAP == theme.SECTION_GAP
        assert _MARGIN == theme.SECTION_GAP

    def test_the_grid_aspect_follows_the_real_frame_ratio(self, window, tmp_path):
        from PyQt5.QtGui import QImage
        from visualizers.palette_visualizer import frame_aspect

        path = tmp_path / "wide.png"
        QImage(240, 100, QImage.Format_RGB32).save(str(path))
        frames = [{"available": True, "image": str(path)}]
        assert abs(frame_aspect(frames) - 2.4) < 0.01

    def test_an_unreadable_frame_falls_back_to_the_default_ratio(self, window):
        from visualizers.palette_visualizer import _DEFAULT_ASPECT, frame_aspect

        assert frame_aspect([]) == _DEFAULT_ASPECT
        assert frame_aspect([{"available": False, "image": "nope.png"}]) == _DEFAULT_ASPECT

    def test_loading_frames_applies_the_ratio_to_the_grid(self, window, tmp_path):
        from PyQt5.QtGui import QImage

        path = tmp_path / "tall.png"
        QImage(100, 200, QImage.Format_RGB32).save(str(path))
        window._browser.set_frames([
            {"index": 0, "shot_id": "s", "image": str(path), "available": True}])
        assert abs(window._browser._grid.aspect() - 0.5) < 0.01

    def test_the_cell_background_is_a_ring_over_the_image(self):
        from visualizers.palette_visualizer import CELL_BORDER_RATIO

        assert CELL_BORDER_RATIO == 0.10

    def test_the_cell_figure_disc_is_one_size_in_both_modes(self, window, tmp_path):
        from PyQt5.QtGui import QImage
        from visualizers.palette_visualizer import CELL_FIGURE_RATIO, _FrameCell

        path = tmp_path / "f.png"
        QImage(240, 100, QImage.Format_RGB32).save(str(path))
        item = {"shot_id": "s", "image": str(path), "available": True,
                "state": store.STATE_ACCEPTED, "preview": [],
                "final": {store.ROLE_FIGURE: {"rgb": [1, 2, 3]},
                          store.ROLE_BACKGROUND: {"rgb": [4, 5, 6]}}}
        cell = _FrameCell(0, item)
        cell.resize(240, 100)
        expected = int(cell.height() * CELL_FIGURE_RATIO)
        for image_on in (True, False):
            cell.set_visibility(True, image_on)
            assert int(cell.height() * CELL_FIGURE_RATIO) == expected

    def test_cells_never_paint_semi_transparently(self):
        import inspect

        from visualizers import palette_visualizer

        source = inspect.getsource(palette_visualizer._FrameCell.paintEvent)
        assert "setOpacity" not in source

    def test_a_thumbnail_is_never_backed_by_a_paler_colour(self):
        """Letterbox slack must read as grid gap, not as an edge on the frame."""
        import inspect

        from visualizers import palette_visualizer

        source = inspect.getsource(palette_visualizer._FrameCell.paintEvent)
        branch = source.split("if pixmap is not None:")[1].split("elif")[0]
        assert "CANVAS_BG" in branch
        assert "CELL_BG" not in branch

    def test_a_thumbnail_fills_its_cell_edge_to_edge(self):
        """The grid truncates width and height apart, so a cell is never
        exactly the media ratio and KeepAspectRatio leaves a bare pixel."""
        import inspect

        from visualizers import palette_visualizer

        source = inspect.getsource(palette_visualizer._FrameCell.paintEvent)
        branch = source.split("if pixmap is not None:")[1].split("elif")[0]
        assert "KeepAspectRatioByExpanding" in branch

    def test_the_grid_gap_and_margin_are_the_canonical_spacing(self):
        from styles import theme
        from visualizers.palette_visualizer import _GAP, _MARGIN

        assert _GAP == _MARGIN == theme.SECTION_GAP

    def test_the_grid_lays_cells_out_on_that_gap(self, window, tmp_path):
        from PyQt5.QtGui import QImage
        from visualizers.palette_visualizer import _GAP

        path = tmp_path / "wide.png"
        QImage(940, 400, QImage.Format_RGB32).save(str(path))
        window._browser.set_frames([
            {"index": i, "shot_id": f"s{i}", "image": str(path), "available": True}
            for i in range(4)])
        grid = window._browser._grid
        grid.resize(800, 600)
        cells = grid.cells()
        if grid.columns() < 2:
            pytest.skip("single column layout")
        first, second = cells[0].geometry(), cells[1].geometry()
        assert second.left() - first.right() - 1 == _GAP

    def test_the_proposal_preview_outlines_nothing(self):
        """A quadrant is a fill, never a bordered sub-element of the cell."""
        import inspect

        from visualizers import palette_visualizer

        source = inspect.getsource(palette_visualizer.paint_preview)
        assert "drawRect" not in source

    def test_only_the_selected_cell_is_outlined(self):
        import inspect

        from visualizers import palette_visualizer

        source = inspect.getsource(palette_visualizer._FrameCell.paintEvent)
        outlines = [line for line in source.splitlines() if "drawRect" in line]
        assert len(outlines) == 1
        assert "if self._selected:" in source

