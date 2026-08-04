"""Smoke tests for SyncVisualizerWindow's WindowVisualizer-based shell.

These exercise only the refactored window/browser/inspector architecture
(Browser fills the splitter alone; Inspector floats as a raised overlay on
top of it instead of occupying its own splitter pane — see
`SyncVisualizerWindow._make_inspector_overlay()` — Tab/h toggle, shared
keyboard shortcuts, workspace zoom) — not the real-time node-graph business
logic (video capture, CLIP embedding, frame matching), which is unchanged
and kept entirely as-is.
"""

import pytest
from PyQt5.QtCore import Qt, QPoint, QPointF
from PyQt5.QtGui import QKeyEvent, QMouseEvent
from PyQt5.QtWidgets import QApplication

from visualizers.sync_visualizer import SyncVisualizerWindow, SyncWorkspace


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


def _make_window(app):
    win = SyncVisualizerWindow()
    app.processEvents()
    return win


def test_window_builds_single_pane_splitter_with_overlay_inspector(app, fake_prefs):
    win = _make_window(app)
    try:
        # The Inspector is detached from the splitter and floats as a
        # raised overlay child of the workspace instead (see
        # _make_inspector_overlay()) — the Browser is the splitter's only
        # real pane, so it alone fills the full window width.
        assert win._splitter.count() == 1
        assert isinstance(win._workspace, SyncWorkspace)
        assert win._inspector_shell is not None
        assert win._inspector_shell.parent() is win._workspace
    finally:
        win.close()


def test_tab_toggles_inspector(app, fake_prefs):
    win = _make_window(app)
    win.show()
    app.processEvents()
    try:
        assert win._inspector_shell.isVisible()

        event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier)
        win.keyPressEvent(event)
        app.processEvents()
        assert win._inspector_hidden is True
        assert not win._inspector_shell.isVisible()

        event2 = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier)
        win.keyPressEvent(event2)
        app.processEvents()
        assert win._inspector_hidden is False
        assert win._inspector_shell.isVisible()
    finally:
        win.close()


def test_h_toggles_inspector_and_node_chrome_together(app, fake_prefs):
    win = _make_window(app)
    win.show()
    app.processEvents()
    try:
        assert win._inspector_shell.isVisible()
        assert win._workspace._chrome_visible is True

        event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_H, Qt.NoModifier)
        win.keyPressEvent(event)
        app.processEvents()
        assert win._inspector_hidden is True
        assert not win._inspector_shell.isVisible()
        assert win._workspace._chrome_visible is False

        event2 = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_H, Qt.NoModifier)
        win.keyPressEvent(event2)
        app.processEvents()
        assert win._inspector_hidden is False
        assert win._inspector_shell.isVisible()
        assert win._workspace._chrome_visible is True
    finally:
        win.close()


def test_escape_closes_window(app, fake_prefs):
    win = _make_window(app)
    win.show()
    app.processEvents()
    assert win.isVisible()

    event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    win.keyPressEvent(event)
    app.processEvents()

    assert not win.isVisible()


def test_zoom_buttons_change_workspace_zoom(app, fake_prefs):
    win = _make_window(app)
    try:
        zm = win._workspace._zoom_manager
        initial = zm.zoom()  # 1.0 default, also the zoom-in ceiling (100%)

        # Already at the zoom-in ceiling, so this must be a no-op.
        win.zoom_in_btn.click()
        app.processEvents()
        assert abs(zm.zoom() - initial) < 1e-6

        win.zoom_out_btn.click()
        app.processEvents()
        assert zm.zoom() < initial

        win.zoom_in_btn.click()
        app.processEvents()
        assert abs(zm.zoom() - initial) < 1e-6
    finally:
        win.close()


def test_zoom_rescales_existing_node_geometry(app, fake_prefs):
    win = _make_window(app)
    try:
        win._workspace.resize(800, 600)   # known size -> known viewport center
        node = win._workspace._make_frame_vector_node()
        node.setGeometry(100, 50, 300, 160)
        node.show()

        anchor = win._workspace.rect().center()

        # Zooming in past the 100% default is disallowed, so exercise the
        # zoom-out direction instead.
        win._change_zoom(-0.5)
        app.processEvents()

        # Rescaled anchored on the viewport center, not the workspace's
        # coordinate-space origin — whatever was at the center stays there.
        ratio = 0.5
        g = node.geometry()
        assert g.width()  == 150   # 300 * 0.5
        assert g.height() == 80    # 160 * 0.5
        assert g.x() == round(anchor.x() + (100 - anchor.x()) * ratio)
        assert g.y() == round(anchor.y() + (50 - anchor.y()) * ratio)
    finally:
        win.close()


def _mouse_event(kind, pos: QPoint, button=Qt.LeftButton) -> QMouseEvent:
    return QMouseEvent(
        kind, QPointF(pos), button,
        button if kind != QMouseEvent.MouseButtonRelease else Qt.NoButton,
        Qt.NoModifier,
    )


def test_drag_on_empty_background_pans_all_nodes(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        node = ws._make_frame_vector_node()
        node.setGeometry(100, 50, 300, 160)
        node.show()
        app.processEvents()

        start = QPoint(700, 500)   # far from the node — empty background
        ws.mousePressEvent(_mouse_event(QMouseEvent.MouseButtonPress, start))
        assert ws._pan_active is True

        moved = QPoint(650, 470)   # drag delta (-50, -30)
        ws.mouseMoveEvent(_mouse_event(QMouseEvent.MouseMove, moved, Qt.NoButton))
        app.processEvents()

        g = node.geometry()
        assert g.x() == 50    # 100 - 50
        assert g.y() == 20    # 50  - 30

        ws.mouseReleaseEvent(_mouse_event(QMouseEvent.MouseButtonRelease, moved))
        assert ws._pan_active is False
    finally:
        win.close()


def test_drag_starting_on_a_port_does_not_pan(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        node = ws._make_frame_vector_node()
        node.setGeometry(100, 50, 300, 160)
        node.show()
        app.processEvents()

        port_pos = node.output_port_pos(node.output_ports()[0])
        ws.mousePressEvent(_mouse_event(QMouseEvent.MouseButtonPress, port_pos))

        assert ws._pan_active is False
        assert ws._drag_src_node is node
    finally:
        win.close()


def test_click_on_node_body_does_not_pan(app, fake_prefs):
    # A click that bubbles up to SyncWorkspace.mousePressEvent (because the
    # node's own body widget under the cursor didn't accept it) must NOT be
    # treated as an "empty background" click just because it wasn't on a
    # port — it landed on top of a node, so no pan should start.
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        node = ws._make_frame_vector_node()
        node.setGeometry(100, 50, 300, 160)
        node.show()
        app.processEvents()

        body_pos = QPoint(node.x() + node.width() // 2, node.y() + node.height() // 2)
        ws.mousePressEvent(_mouse_event(QMouseEvent.MouseButtonPress, body_pos))

        assert ws._pan_active is False
    finally:
        win.close()


def test_pan_drag_is_clamped_near_viewport_edge(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        node = ws._make_frame_vector_node()
        node.setGeometry(100, 50, 300, 160)
        node.show()
        app.processEvents()

        start = QPoint(700, 500)
        ws.mousePressEvent(_mouse_event(QMouseEvent.MouseButtonPress, start))

        # At default zoom, the world bounds' 1000px minimum (_WORLD_MIN_W/H)
        # is bigger than this 800x600 viewport's safe zone, so it can't fit
        # — a huge drag falls back to "keep some part reachable": whichever
        # edge is nearer gets pinned at the margin line, and the far edge
        # is free to sit offscreen.
        moved = QPoint(start.x() - 5000, start.y() - 5000)
        ws.mouseMoveEvent(_mouse_event(QMouseEvent.MouseMove, moved, Qt.NoButton))
        app.processEvents()

        # ±1px tolerance: the surrounding splitter can still be settling
        # ws's exact pixel width/height across a frame or two under the
        # offscreen QPA platform, which shifts the (rounded) margin by a
        # pixel between when the clamp ran and when we check it here.
        margin_x = ws._pan_margin_x()
        margin_y = ws._pan_margin_y()
        g = ws._world_rect()
        assert abs(g.right()  - margin_x) <= 1
        assert abs(g.bottom() - margin_y) <= 1

        ws.mouseReleaseEvent(_mouse_event(QMouseEvent.MouseButtonRelease, moved))
    finally:
        win.close()


def test_pan_drag_clamps_top_left_corner_when_world_bounds_fits_viewport(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        node = ws._make_frame_vector_node()
        node.setGeometry(100, 50, 300, 160)
        node.show()
        app.processEvents()

        # Zoom all the way out — the world bounds' minimum shrinks well
        # below the viewport size, so it comfortably fits within the safe
        # zone (see the module docstring on _clamp_pan_delta()'s "fits"
        # case).
        win._change_zoom(-0.9)
        app.processEvents()

        start = QPoint(700, 500)
        ws.mousePressEvent(_mouse_event(QMouseEvent.MouseButtonPress, start))

        # A huge drag toward the top-left — the world bounds' own top-left
        # corner should stop exactly at the 5%-inset line, never crossing
        # past it (never fully offscreen), matching the "5% internal
        # padding" behavior at max zoom-out.
        moved = QPoint(start.x() - 5000, start.y() - 5000)
        ws.mouseMoveEvent(_mouse_event(QMouseEvent.MouseMove, moved, Qt.NoButton))
        app.processEvents()

        # ±1px tolerance — see test_pan_drag_is_clamped_near_viewport_edge.
        margin_x = ws._pan_margin_x()
        margin_y = ws._pan_margin_y()
        vp_w = ws.width()
        g = ws._world_rect()
        assert g.width() <= vp_w - 2 * margin_x
        assert abs(g.left() - margin_x) <= 1
        assert abs(g.top()  - margin_y) <= 1

        ws.mouseReleaseEvent(_mouse_event(QMouseEvent.MouseButtonRelease, moved))
    finally:
        win.close()


def test_inspector_overlay_does_not_narrow_workspace_width(app, fake_prefs):
    # The Inspector used to be a real splitter pane that shrank the Browser
    # pane's width by its own — it's now a floating overlay raised on top
    # of the workspace instead (see _make_inspector_overlay()), so the
    # workspace's own scrollable/pannable extent should always match the
    # full splitter width, whether or not the Inspector is visible.
    win = _make_window(app)
    win.show()
    try:
        win.resize(900, 600)
        app.processEvents()

        assert win._inspector_shell.isVisible()
        assert win._workspace.width() == win._splitter.width()

        # Toggling the Inspector hidden/visible must not change this either.
        win._toggle_inspector()
        app.processEvents()
        assert win._workspace.width() == win._splitter.width()

        win._toggle_inspector()
        app.processEvents()
        assert win._workspace.width() == win._splitter.width()
    finally:
        win.close()


def test_inspector_overlay_is_raised_above_the_canvas(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        win.resize(900, 600)
        app.processEvents()

        ws = win._workspace
        shell = win._inspector_shell
        assert shell.parent() is ws
        assert shell in ws._chrome_overlays

        # The overlay sits flush against the right edge, spanning the full
        # workspace height, rather than being squeezed into a separate
        # splitter pane.
        g = shell.geometry()
        assert g.right()  == ws.width() - 1
        assert g.height() == ws.height()

        # Adding a new node afterward must not bury the overlay underneath
        # it — childEvent() re-raises registered chrome overlays whenever a
        # new child (like a node) is added.
        node = ws._make_frame_vector_node()
        node.setGeometry(ws.width() - 200, 0, 300, 160)
        node.show()
        app.processEvents()
        assert ws._chrome_overlays[-1].isVisible()
    finally:
        win.close()


def test_click_on_inspector_overlay_does_not_pan(app, fake_prefs):
    # A click that lands on the Inspector's own (non-interactive) background
    # bubbles up to SyncWorkspace.mousePressEvent exactly like a node-body
    # click does — it must not be mistaken for an empty-background click
    # that starts a canvas pan.
    win = _make_window(app)
    win.show()
    try:
        win.resize(900, 600)
        app.processEvents()

        ws = win._workspace
        shell = win._inspector_shell
        click_pos = QPoint(shell.x() + shell.width() // 2, shell.y() + 5)
        ws.mousePressEvent(_mouse_event(QMouseEvent.MouseButtonPress, click_pos))

        assert ws._pan_active is False
    finally:
        win.close()


def test_nodes_can_be_panned_underneath_the_inspector_overlay(app, fake_prefs):
    # The whole point of floating the Inspector instead of giving it its
    # own splitter pane: the canvas's pannable area spans the full window,
    # so a node can end up positioned underneath the Inspector's footprint
    # exactly as if the Inspector weren't there.
    win = _make_window(app)
    win.show()
    try:
        win.resize(900, 600)
        app.processEvents()

        ws = win._workspace
        shell = win._inspector_shell
        node = ws._make_frame_vector_node()
        node.setGeometry(100, 50, 300, 160)
        node.show()
        app.processEvents()

        start = QPoint(400, 300)
        ws.mousePressEvent(_mouse_event(QMouseEvent.MouseButtonPress, start))
        # A modest drag (not the huge, edge-clamped drags used elsewhere in
        # this file) so the node lands squarely under the Inspector's
        # footprint instead of getting pinned by the reachability clamp.
        moved = QPoint(start.x() + 400, start.y())
        ws.mouseMoveEvent(_mouse_event(QMouseEvent.MouseMove, moved, Qt.NoButton))
        app.processEvents()
        ws.mouseReleaseEvent(_mouse_event(QMouseEvent.MouseButtonRelease, moved))

        assert node.geometry().intersects(shell.geometry())
    finally:
        win.close()


def test_inspector_grip_handle_sits_flush_against_the_overlay(app, fake_prefs):
    # The grip handle stands in for a real GripSplitter handle (see
    # _InspectorGripHandle) — registered as a chrome overlay (so a click on
    # it doesn't start a canvas pan) and always positioned immediately to
    # the left of the floating Inspector shell.
    win = _make_window(app)
    win.show()
    try:
        win.resize(900, 600)
        app.processEvents()

        ws = win._workspace
        grip = win._inspector_grip
        shell = win._inspector_shell
        assert grip in ws._chrome_overlays
        assert grip.geometry().right() == shell.geometry().left() - 1
        assert grip.height() == ws.height()
    finally:
        win.close()


def test_click_on_inspector_grip_handle_collapses_and_expands(app, fake_prefs):
    # Grip-handle clicks only *collapse* the Inspector (`_inspector_collapsed`)
    # — distinct from Tab/'h', which *hide* it entirely (`_inspector_hidden`).
    win = _make_window(app)
    win.show()
    try:
        win.resize(900, 600)
        app.processEvents()

        grip = win._inspector_grip
        pos = QPoint(grip.width() // 2, grip.height() // 2)

        assert win._inspector_collapsed is False
        grip.mousePressEvent(_mouse_event(QMouseEvent.MouseButtonPress, pos))
        grip.mouseReleaseEvent(_mouse_event(QMouseEvent.MouseButtonRelease, pos))
        assert win._inspector_collapsed is True
        assert win._inspector_hidden is False
        assert win._inspector_shell.isVisible() is False
        # The handle itself stays visible/flush against the right edge so
        # it can still be clicked to re-expand.
        assert win._inspector_grip.isVisible() is True
        assert win._inspector_grip.geometry().right() == win._workspace.width() - 1

        grip.mousePressEvent(_mouse_event(QMouseEvent.MouseButtonPress, pos))
        grip.mouseReleaseEvent(_mouse_event(QMouseEvent.MouseButtonRelease, pos))
        assert win._inspector_collapsed is False
        assert win._inspector_shell.isVisible() is True
    finally:
        win.close()


def test_tab_hides_inspector_and_grip_handle_entirely(app, fake_prefs):
    # Tab hides the Inspector AND the grip handle entirely — no visible
    # remnant, unlike the grip handle's own click (which only collapses the
    # Inspector while leaving the grip itself visible/clickable).
    win = _make_window(app)
    win.show()
    try:
        win.resize(900, 600)
        app.processEvents()

        assert win._inspector_grip.isVisible() is True

        event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier)
        win.keyPressEvent(event)
        app.processEvents()
        assert win._inspector_hidden is True
        assert win._inspector_collapsed is False
        assert win._inspector_shell.isVisible() is False
        assert win._inspector_grip.isVisible() is False

        event2 = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier)
        win.keyPressEvent(event2)
        app.processEvents()
        assert win._inspector_hidden is False
        assert win._inspector_shell.isVisible() is True
        assert win._inspector_grip.isVisible() is True
    finally:
        win.close()


def test_drag_on_inspector_grip_handle_resizes_inspector(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        win.resize(900, 600)
        app.processEvents()

        grip = win._inspector_grip
        start_width = win._inspector_width
        start = QPoint(grip.width() // 2, grip.height() // 2)
        # Drag left (toward the canvas) by 40px — dragging the handle's own
        # left edge further left widens the Inspector overlay.
        moved = QPoint(start.x() - 40, start.y())

        grip.mousePressEvent(_mouse_event(QMouseEvent.MouseButtonPress, start))
        grip.mouseMoveEvent(_mouse_event(QMouseEvent.MouseMove, moved))
        assert win._inspector_width == start_width + 40
        grip.mouseReleaseEvent(_mouse_event(QMouseEvent.MouseButtonRelease, moved))

        # A real drag must not also toggle the Inspector's visibility.
        assert win._inspector_hidden is False
        assert win._inspector_shell.isVisible() is True
        assert win._inspector_shell.geometry().width() == start_width + 40
    finally:
        win.close()


def test_world_rect_has_1000px_minimum_at_default_zoom(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        app.processEvents()

        # No nodes at all — falls back to the minimum box, centered in the
        # viewport.
        g = ws._world_rect()
        assert g.width()  == 1000
        assert g.height() == 1000
        assert abs(g.center().x() - ws.rect().center().x()) <= 1
        assert abs(g.center().y() - ws.rect().center().y()) <= 1

        # A single small node well inside that minimum shouldn't grow it.
        node = ws._make_frame_vector_node()
        node.setGeometry(100, 50, 300, 160)
        node.show()
        app.processEvents()
        g2 = ws._world_rect()
        assert g2.width()  == 1000
        assert g2.height() == 1000
    finally:
        win.close()


def test_world_rect_expands_to_include_widely_spaced_nodes(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        # A single small node is always contained within a 1000x1000 box
        # centered on it, so expansion only shows up once the *combined*
        # bounding box of all nodes exceeds that minimum footprint.
        node1 = ws._make_frame_vector_node()
        node1.setGeometry(0, 0, 300, 160)
        node1.show()
        node2 = ws._make_frame_vector_node()
        node2.setGeometry(5000, 5000, 300, 160)
        node2.show()
        app.processEvents()

        g = ws._world_rect()
        bbox = ws._nodes_bounding_rect()
        assert g.contains(bbox)
        assert g.width()  > 1000
        assert g.height() > 1000
    finally:
        win.close()


def test_world_rect_shrinks_back_to_minimum_after_node_removed(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        node1 = ws._make_frame_vector_node()
        node1.setGeometry(0, 0, 300, 160)
        node1.show()
        node2 = ws._make_frame_vector_node()
        node2.setGeometry(5000, 5000, 300, 160)
        node2.show()
        app.processEvents()
        assert ws._world_rect().width() > 1000

        node2._on_close()
        app.processEvents()

        g = ws._world_rect()
        assert g.width()  == 1000
        assert g.height() == 1000
    finally:
        win.close()


def test_title_bar_drag_moves_only_that_node_and_does_not_pan(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        node1 = ws._make_frame_vector_node()
        node1.setGeometry(100, 50, 300, 160)
        node1.show()
        node2 = ws._make_frame_vector_node()
        node2.setGeometry(500, 400, 300, 160)
        node2.show()
        app.processEvents()

        # Passing an explicit globalPos (6-arg ctor) so the delta the
        # eventFilter computes from consecutive globalPos() calls below is
        # exactly the literal delta used here (the 5-arg ctor instead reads
        # globalPos() from the real QCursor::pos(), which doesn't move in
        # this headless test).
        tb = node1._title_bar
        start = QPoint(500, 500)
        press = QMouseEvent(
            QMouseEvent.MouseButtonPress, QPointF(start), QPointF(start),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        assert node1.eventFilter(tb, press) is True
        assert ws._pan_active is False

        moved = start + QPoint(40, 25)
        move_evt = QMouseEvent(
            QMouseEvent.MouseMove, QPointF(moved), QPointF(moved),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
        )
        assert node1.eventFilter(tb, move_evt) is True
        app.processEvents()

        # Only node1 moved (by the drag delta) — node2 must be untouched,
        # and the workspace must never have entered its own pan mode.
        assert node1.geometry().x() == 140   # 100 + 40
        assert node1.geometry().y() == 75    # 50  + 25
        assert node2.geometry().x() == 500
        assert node2.geometry().y() == 400
        assert ws._pan_active is False

        release = QMouseEvent(
            QMouseEvent.MouseButtonRelease, QPointF(moved), QPointF(moved),
            Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
        )
        assert node1.eventFilter(tb, release) is True
    finally:
        win.close()


def test_recenter_button_centers_node_bounding_box(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        node = ws._make_frame_vector_node()
        node.setGeometry(1500, 1200, 300, 160)
        node.show()
        app.processEvents()

        ws.recenter_view()
        app.processEvents()

        g = node.geometry()
        bbox_center_x = g.x() + g.width() / 2
        bbox_center_y = g.y() + g.height() / 2
        assert abs(bbox_center_x - ws.width()  / 2) < 1
        assert abs(bbox_center_y - ws.height() / 2) < 1
    finally:
        win.close()


def test_clear_button_removes_nodes_and_resets_zoom(app, fake_prefs):
    win = _make_window(app)
    win.show()
    try:
        ws = win._workspace
        ws.resize(800, 600)
        node1 = ws._make_frame_vector_node()
        node1.setGeometry(100, 50, 300, 160)
        node1.show()
        node2 = ws._make_frame_vector_node()
        node2.setGeometry(500, 400, 300, 160)
        node2.show()
        app.processEvents()

        win._change_zoom(-0.5)
        app.processEvents()
        assert abs(ws._zoom_manager.zoom() - 0.5) < 1e-6

        win._on_clear_clicked()
        app.processEvents()

        assert ws.nodes() == []
        assert ws.connections_state() == []
        assert abs(ws._zoom_manager.zoom() - 1.0) < 1e-6
        assert not win.zoom_in_btn.isEnabled()   # already at the 100% ceiling
        assert win.zoom_out_btn.isEnabled()
    finally:
        win.close()

