"""Regression tests for the cross-process single-instance guard.

Bug: users reported multiple instances of the same visualizer (e.g.
Metadata) open at once, in different window states (fullscreen, windowed),
after using the F1-F10/F12 function keys. `raise_existing_window()` only
prevents duplicates *within one process* (same `QApplication`) — it has no
way to see a window living in a different OS process. Since every
visualizer can also be launched directly (`crossing visualizer metadata`,
its own process), a second process pressing (say) F2 had no way to know
Metadata was already open elsewhere, and happily created a second window.

`visualizers.components.singleton_guard` closes that gap with a generic,
reusable Unix-domain-socket claim-or-ping primitive (the same pattern
already used by Illustration's navigate socket and Shotlist's IPC, which
are intentionally excluded here via `SELF_MANAGED_SUBCOMMANDS` since they
already have their own bespoke cross-process mechanism).
"""

import json
import socket
import time

import pytest
from PyQt5.QtWidgets import QApplication

from visualizers.components.singleton_guard import (
    SELF_MANAGED_SUBCOMMANDS,
    _guard_socket_path,
    claim_or_ping,
    claim_or_ping_and_bind,
)


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


def _pump(app, ticks: int = 10, delay: float = 0.02) -> None:
    for _ in range(ticks):
        app.processEvents()
        time.sleep(delay)


def _stop_guard(guard) -> None:
    if guard is not None:
        guard.stop()
        guard.wait(2000)


def test_claim_or_ping_claims_when_unowned(app, tmp_path):
    """No other process owns this subcommand/project yet: claim it."""
    guard = claim_or_ping("metadata", str(tmp_path))
    try:
        assert guard is not None
        sock_path = _guard_socket_path("metadata", str(tmp_path))
        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.02)
        assert sock_path.exists()
    finally:
        _stop_guard(guard)


def test_claim_or_ping_pings_existing_owner_instead_of_claiming(app, tmp_path):
    """Simulate another process already owning the socket: claim_or_ping()
    must not return a guard, and the "owner" must receive a raise ping."""
    sock_path = _guard_socket_path("metadata", str(tmp_path))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    try:
        result = claim_or_ping("metadata", str(tmp_path))
        assert result is None

        conn, _ = srv.accept()
        data = conn.recv(4096)
        conn.close()
        assert json.loads(data.decode()) == {"action": "raise"}
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)


def test_switch_to_visualizer_does_not_duplicate_across_processes(app, fake_prefs, tmp_path):
    """The core reported bug: pressing an F-key for a visualizer already
    open in a *different* OS process must not create a second window here.
    """
    fake_prefs["path"] = str(tmp_path)

    sock_path = _guard_socket_path("metadata", str(tmp_path))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    try:
        from visualizers._window_helpers import switch_to_visualizer
        from visualizers.metadata_visualizer import MetadataVisualizer

        switch_to_visualizer("metadata")
        _pump(app)

        metas = [w for w in app.topLevelWidgets() if isinstance(w, MetadataVisualizer)]
        assert len(metas) == 0, "must not create a duplicate Metadata window in this process"

        # The "other process" (our fake socket) must have received the ping.
        srv.settimeout(2.0)
        conn, _ = srv.accept()
        data = conn.recv(4096)
        conn.close()
        assert json.loads(data.decode()) == {"action": "raise"}
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)


def test_owning_process_raises_existing_window_on_external_ping(app, fake_prefs, tmp_path):
    """When this process legitimately owns a visualizer, an external raise
    ping (from a would-be second process) must re-raise the existing
    window rather than anything creating a duplicate.
    """
    fake_prefs["path"] = str(tmp_path)

    from visualizers._window_helpers import switch_to_visualizer
    from visualizers.metadata_visualizer import MetadataVisualizer

    switch_to_visualizer("metadata")
    _pump(app)
    metas = [w for w in app.topLevelWidgets() if isinstance(w, MetadataVisualizer)]
    assert len(metas) == 1
    win = metas[0]
    win.hide()
    _pump(app)
    assert not win.isVisible()

    sock_path = _guard_socket_path("metadata", str(tmp_path))
    assert sock_path.exists()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(2.0)
    conn.connect(str(sock_path))
    conn.sendall(json.dumps({"action": "raise"}).encode())
    conn.close()

    for _ in range(50):
        _pump(app, ticks=1, delay=0.02)
        if win.isVisible():
            break

    assert win.isVisible()
    metas_after = [w for w in app.topLevelWidgets() if isinstance(w, MetadataVisualizer)]
    assert len(metas_after) == 1, "external ping must raise the existing window, not create another"

    win.close()
    _stop_guard(app._singleton_guards.pop("metadata", None))


def test_claim_or_ping_and_bind_is_idempotent_per_app(app, tmp_path):
    """A second call for a subcommand this process already owns must
    return True immediately without re-claiming or erroring."""
    fake_app = QApplication.instance()
    assert claim_or_ping_and_bind("cloud", str(tmp_path), fake_app) is True
    assert claim_or_ping_and_bind("cloud", str(tmp_path), fake_app) is True
    _stop_guard(fake_app._singleton_guards.pop("cloud", None))


def test_illustration_and_shotlist_are_self_managed() -> None:
    """Illustration and Shotlist already have their own cross-process IPC
    (navigation payloads, not just raising) and must be excluded from the
    generic guard to avoid a second, redundant socket for those two.
    """
    assert "illustration" in SELF_MANAGED_SUBCOMMANDS
    assert "shotlist" in SELF_MANAGED_SUBCOMMANDS


def test_switch_to_visualizer_project_ignores_hidden_hub_from_other_subcommand(
    app, fake_prefs, tmp_path
):
    """Bug: pressing another F-key first (e.g. F10 for Book) bootstraps a
    hidden, never-shown `ProjectVisualizer` "hub" in this process purely to
    call `_launch()` on. That hub matches "project"'s window title, so a
    later F1 press used to be swallowed by the generic
    `raise_existing_window("project")` scan at the top of
    `switch_to_visualizer` — which just showed the hidden hub instead of
    pinging whichever process (this one, or a real other one) actually has
    Project legitimately open, producing a genuine duplicate the moment a
    *different* process's Project window was also raised. The hub must be
    ignored until it has actually been shown as Project at least once.
    """
    fake_prefs["path"] = str(tmp_path)

    from visualizers._window_helpers import switch_to_visualizer
    from visualizers.project_visualizer import ProjectVisualizer

    # Simulate another OS process already legitimately owning "project".
    sock_path = _guard_socket_path("project", str(tmp_path))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    srv.settimeout(2.0)
    try:
        # F10 (Book) bootstraps a hidden Project hub as a side effect.
        switch_to_visualizer("book")
        _pump(app)
        hubs = [w for w in app.topLevelWidgets() if isinstance(w, ProjectVisualizer)]
        assert len(hubs) == 1
        assert hubs[0].isVisible() is False
        assert hubs[0]._shown_as_project is False

        # F1 (Project) must not reveal that hidden hub — it must instead
        # detect the other process owns "project" and ping it.
        switch_to_visualizer("project")
        _pump(app)
        hubs_after = [w for w in app.topLevelWidgets() if isinstance(w, ProjectVisualizer)]
        assert len(hubs_after) == 1
        assert hubs_after[0].isVisible() is False, "must not reveal the hidden hub"

        conn, _ = srv.accept()
        data = conn.recv(4096)
        conn.close()
        assert json.loads(data.decode()) == {"action": "raise"}
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)
