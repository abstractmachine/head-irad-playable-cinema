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


def _generated(window, choices=("1", "2")):
    """Give the current frame stored proposals, through the real store."""
    from services import palette_review as service

    record = store.load_review(window._project_path, window._filename,
                               window._media_type)
    entry = store.frame(record, window.current_frame()["shot_id"])
    store.record_generation(
        entry, service.PROPOSAL_VERSION, "gen-test",
        {key: {"strategy": "direct", "label": "DIRECT", "active": True,
               "materials": ["a", "b"], "converges_with": [],
               "colours": [{"rgb": [1, 2, 3], "hex": "#010203"},
                           {"rgb": [4, 5, 6], "hex": "#040506"}]}
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
        labels = {mode: button.text()
                  for mode, button in window._mode_btns.items()}
        assert labels == {"single": "Single Frame   S", "all": "All Frames   A"}
        assert all(button.isVisibleTo(window._inspector)
                   for button in window._mode_btns.values())

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

    def test_the_scrub_area_does_not_leave_a_tall_empty_band(self, window):
        from styles import theme
        from visualizers.palette_visualizer import SCRUB_BARS

        scrub = window._browser._scrub
        assert scrub.height() == theme.SCROLLBAR_W * SCRUB_BARS
        assert SCRUB_BARS < 15

    def test_the_scrub_area_paints_the_browser_background(self, window):
        from styles import theme

        scrub = window._browser._scrub
        # A bare QWidget ignores an ancestor's stylesheet without this, and
        # falls back to the light window grey.
        assert scrub.testAttribute(Qt.WA_StyledBackground) is True
        assert theme.CANVAS_BG in scrub.styleSheet()
        assert theme.BG not in scrub.styleSheet()

    def test_the_browser_stack_absorbs_the_remaining_height(self, window):
        page = window._browser
        page.resize(800, 600)
        page.layout().activate()
        assert page._stack.height() + page._scrub.height() == page.height()


class TestKeyboardModel:
    def test_c_creates_proposals_and_never_changes_display_mode(self, window, monkeypatch):
        called = []
        monkeypatch.setattr(window, "create_palette", lambda: called.append(True))
        before = window.mode()
        _press(window, Qt.Key_C, "c")
        assert called == [True]
        assert window.mode() == before

    def test_c_is_not_bound_to_any_display_action(self, window):
        before = (window.mode(), window._show_image, window._show_palette)
        window._worker = object()          # suppress real generation
        _press(window, Qt.Key_C, "c")
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
    def test_a_valid_choice_accepts_and_advances(self, window):
        _generated(window)
        assert window.accept_choice("1") is True
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type)["frames"]["first.mkv@0"]
        assert entry["review"]["choice"] == "1"
        assert window._current == 1

    def test_a_missing_choice_does_nothing(self, window):
        _generated(window, choices=("1",))
        before = window._current
        assert window.accept_choice("3") is False
        assert window._current == before

    def test_an_ungenerated_frame_accepts_nothing(self, window):
        before = window._current
        assert window.accept_choice("1") is False
        assert window._current == before

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

    def test_rejection_is_persisted_and_advances(self, window):
        _generated(window)
        window.reject_proposals()
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type)["frames"]["first.mkv@0"]
        assert entry["review"]["answer"] == store.ANSWER_REJECTED
        assert window._current == 1

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
    def test_a_single_region_can_be_selected(self, window):
        canvas = window._browser.canvas()
        canvas.set_blobs([{"polygon": [[0, 0], [1, 0], [1, 1]], "area": 3, "mask": None}])
        canvas.toggle_selection(0, additive=False)
        assert canvas.selected_indices() == [0]

    def test_shift_accumulates_regions(self, window):
        canvas = window._browser.canvas()
        canvas.set_blobs([{"polygon": [], "area": 1, "mask": None} for _ in range(3)])
        canvas.toggle_selection(0, additive=False)
        canvas.toggle_selection(2, additive=True)
        assert canvas.selected_indices() == [0, 2]

    def test_a_selected_region_can_be_removed(self, window):
        canvas = window._browser.canvas()
        canvas.set_blobs([{"polygon": [], "area": 1, "mask": None} for _ in range(2)])
        canvas.toggle_selection(0, additive=False)
        canvas.toggle_selection(1, additive=True)
        canvas.toggle_selection(0, additive=True)
        assert canvas.selected_indices() == [1]

    def test_masks_are_never_unioned_automatically(self, window):
        from visualizers.palette_visualizer import build_blobs
        import numpy as np

        masks = [{"segmentation": np.ones((6, 6), dtype=bool)},
                 {"segmentation": np.zeros((6, 6), dtype=bool)}]
        masks[1]["segmentation"][0:3, 0:3] = True
        blobs = build_blobs(masks)
        assert len(blobs) == 2
        assert window._browser.canvas().selected_indices() == []

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

    def test_assigning_with_no_selection_does_not_persist_anything(self, window):
        window._browser.canvas().clear_selection()
        window.assign_role(store.ROLE_FIGURE)
        entry = store.load_review(window._project_path, window._filename,
                                  window._media_type).get("frames", {})
        assert "manual" not in entry.get("first.mkv@0", {})


class TestDisplayToggles:
    def test_the_inspector_exposes_palette_and_image_toggles(self, window):
        labels = {name: button.text()
                  for name, button in window._toggle_btns.items()}
        assert labels == {"palette": "Palette   P", "image": "Image   I"}

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
        assert quadrants[0]["background"]["hex"] == "#040506"

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

    def test_progress_does_not_reopen_the_status_row(self, window):
        window._on_progress(1, 1, "shot", "generated")
        assert window._progress_lbl.isVisible() is False

    def test_the_title_returns_to_plain_when_generation_stops(self, window):
        window._on_progress(1, 1, "shot", "generated")
        window._stop_worker()
        assert window._generate_section._header.text() == "Generate"


class TestInspectorLayout:
    def test_generate_contains_only_intentional_controls(self, window):
        from PyQt5.QtWidgets import QPushButton

        container = window._create_btn.parentWidget()
        buttons = [child for child in container.children()
                   if isinstance(child, QPushButton)]
        assert [button.text() for button in buttons] == [
            "Create Palette   C", "Create All Palettes"]
        assert all(button.text().strip() for button in buttons)

    def test_the_generate_progress_row_is_hidden_while_empty(self, window):
        assert window._progress_lbl.text() == ""
        assert window._progress_lbl.isVisible() is False

    def test_the_progress_row_appears_only_when_it_has_something_to_say(self, window):
        window._set_progress("Generating palettes\n3 / 9")
        assert window._progress_lbl.isVisibleTo(window._progress_lbl.parentWidget())
        window._set_progress("")
        assert window._progress_lbl.isVisible() is False

    def test_manual_has_no_explanatory_prose_block(self, window):
        from PyQt5.QtWidgets import QLabel

        container = window._manual_lbl.parentWidget()
        for label in container.findChildren(QLabel):
            assert len(label.text()) < 60, label.text()
        assert window._manual_lbl.text() == ""
        assert window._manual_lbl.isVisible() is False

    def test_manual_contains_the_two_role_controls(self, window):
        from PyQt5.QtWidgets import QPushButton

        container = window._manual_lbl.parentWidget()
        buttons = [child for child in container.children()
                   if isinstance(child, QPushButton)]
        assert [button.text() for button in buttons] == [
            "Figure   F", "Background   B"]

    def test_the_manual_instructions_live_in_tooltips(self, window):
        from PyQt5.QtWidgets import QPushButton
        from visualizers.palette_visualizer import MANUAL_HELP

        container = window._manual_lbl.parentWidget()
        assert container.toolTip() == MANUAL_HELP
        for button in container.findChildren(QPushButton):
            assert button.toolTip() == MANUAL_HELP
        for phrase in ("Shift+Click", "Alt+Click", "Ctrl+Click",
                       "click the frame to segment"):
            assert phrase.lower() in MANUAL_HELP.lower()

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


class TestMosaicState:
    def test_every_state_has_a_distinct_mosaic_mark(self):
        from visualizers.palette_visualizer import STATE_COLOURS

        assert set(STATE_COLOURS) == set(store.STATES)
        assert len(set(STATE_COLOURS.values())) == len(store.STATES)
