"""Cross-process single-instance guard for visualizer windows.

`visualizers._window_helpers.raise_existing_window()` already guarantees at
most one window per subcommand *within a single process* (same
`QApplication`). It cannot see windows living in a different OS process,
which is how duplicate visualizer windows can appear: e.g. the user opens
Metadata directly (`crossing visualizer metadata`, its own process), then
later presses F2 from inside a different already-running visualizer's
process — that process has no way to know Metadata is already open
elsewhere, so it happily creates a second one.

This module extends the single-instance guarantee across processes using
the same Unix-domain-socket pattern already used by the Illustration
Visualizer (`_IllIpcServer` in `illustration_visualizer.py`) and Shotlist.
Rather than duplicating that per-visualizer, message-carrying IPC server
for every remaining visualizer, this provides one generic, reusable
claim-or-ping primitive: whichever process gets there first "owns" the
socket for (subcommand, project_path); every later process that tries to
open the same subcommand for the same project instead pings the owner to
raise its window and does not create one of its own.

Illustration and Shotlist are excluded (`SELF_MANAGED_SUBCOMMANDS`) since
they already have their own bespoke cross-process mechanisms that also
carry navigation payloads, not just a raise signal; adding this generic
guard on top would just be a second, redundant socket for those two.
"""

from __future__ import annotations

import hashlib
import json
import socket
import tempfile
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import pyqtSignal

from visualizers.components.ipc_server import IpcServer

# Subcommands with their own bespoke cross-process IPC (navigation payloads,
# not just single-instance raising) — must not also go through this generic
# guard, to avoid two competing sockets for the same visualizer.
SELF_MANAGED_SUBCOMMANDS = {"illustration", "shotlist"}


def _guard_socket_path(subcommand: str, project_path: str) -> Path:
    """Per-(subcommand, project) socket file path, stable across processes."""
    h = hashlib.md5(str(project_path).encode()).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"crossing_{subcommand}_{h}.sock"


class _SingletonGuardServer(IpcServer):
    """Listens for "raise" pings from other processes and re-emits them as
    a Qt signal on the GUI thread so the owning process can raise its
    already-open window (via `raise_existing_window`).
    """

    raise_requested = pyqtSignal()

    def __init__(self, subcommand: str, project_path: str, parent=None) -> None:
        super().__init__(_guard_socket_path(subcommand, project_path), parent)

    def _handle_message(self, msg: dict) -> None:
        if msg.get("action") == "raise":
            self.raise_requested.emit()


def claim_or_ping(subcommand: str, project_path: str) -> Optional[_SingletonGuardServer]:
    """Claim ownership of *subcommand* for *project_path* in this process,
    or ping the process that already owns it.

    Returns a started `_SingletonGuardServer` if this process should
    proceed to construct the window (caller must connect
    `guard.raise_requested` and keep the guard referenced for the process's
    lifetime, e.g. via `claim_or_ping_and_bind`). Returns None if another
    process already owns *subcommand* — that process has just been pinged
    to raise its window and the caller must NOT construct a new one.
    """
    sock_path = _guard_socket_path(subcommand, project_path)

    if sock_path.exists():
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(2.0)
            conn.connect(str(sock_path))
            conn.sendall(json.dumps({"action": "raise"}).encode())
            conn.close()
            return None  # delivered — another process owns it
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            pass  # stale socket file; fall through and claim it ourselves

    guard = _SingletonGuardServer(subcommand, project_path)
    guard.start()
    return guard


def claim_or_ping_and_bind(subcommand: str, project_path: str, app) -> bool:
    """Ensure *this process* owns the cross-process guard for *subcommand*/
    *project_path*, claiming it if free or pinging the owning process to
    raise its window if not.

    Returns True if this process owns (or already owned) *subcommand* and
    the caller should proceed to create/show its window. Returns False if
    another process already owns it; that process has been pinged to raise
    its window and the caller must not create a new one.

    Idempotent per `(app, subcommand)`: a subsequent call for a subcommand
    this process already owns just returns True without re-claiming.
    """
    guards = getattr(app, "_singleton_guards", None)
    if guards is None:
        guards = {}
        app._singleton_guards = guards

    if subcommand in guards:
        return True

    guard = claim_or_ping(subcommand, project_path)
    if guard is None:
        return False

    from visualizers._window_helpers import raise_existing_window
    guard.raise_requested.connect(lambda: raise_existing_window(subcommand))
    guards[subcommand] = guard
    return True
