"""IpcServer — reusable Unix-socket bridge for visualizer windows.

Provides single-instance window management and external navigation handoff.
Subclasses parse small routing messages and emit UI-thread signals.

This class transports messages only. It does not implement project/business
operations; those remain in services and CLI commands.

Both existing IPC implementations follow the same structure:

- ``_IpcServer``    (shot_visualizer.py)       — navigate to shot on load
- ``_SilIpcServer`` (silhouette_visualizer.py) — navigate to label/field

Migration of existing callers to subclass IpcServer is deferred to a later
phase.  The Shot Visualizer is explicitly out of scope for the initial
framework migration.

Usage::

    class MyIpcServer(IpcServer):
        navigateRequested = pyqtSignal(str, str)   # label, field

        def _handle_message(self, msg: dict) -> None:
            if msg.get("action") == "navigate":
                self.navigateRequested.emit(
                    msg.get("label", ""),
                    msg.get("field", ""),
                )

    server = MyIpcServer(socket_path, project_path)
    server.start()
    ...
    server.stop()
    server.wait()
"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt5.QtCore import QThread


class IpcServer(QThread):
    """Base Unix-domain socket server for visualizer coordination.

    Binds a Unix socket at *socket_path*, accepts connections one at a time,
    reads a complete JSON message, and dispatches to ``_handle_message``.

    Parameters
    ----------
    socket_path:
        Path to the Unix-domain socket file.  The file is created on start
        and removed on stop.
    parent:
        Optional parent QObject.
    """

    def __init__(self, socket_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._socket_path = socket_path
        self._running     = True

    # ------------------------------------------------------------------ lifecycle

    def stop(self) -> None:
        """Signal the server to stop accepting connections.  Non-blocking."""
        self._running = False

    # ------------------------------------------------------------------ subclass API

    def _handle_message(self, msg: dict) -> None:
        """Called on the server thread with the parsed JSON message.

        Subclasses override this to emit their own Qt signals via
        ``QMetaObject.invokeMethod`` or by using ``pyqtSignal`` (which
        automatically marshals across threads).
        """

    # ------------------------------------------------------------------ run loop

    def run(self) -> None:
        import socket as _socket

        # Remove stale socket file if present.
        try:
            self._socket_path.unlink()
        except FileNotFoundError:
            pass

        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            srv.bind(str(self._socket_path))
            srv.listen(5)
            srv.settimeout(1.0)

            while self._running:
                try:
                    conn, _ = srv.accept()
                except _socket.timeout:
                    continue

                try:
                    data = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    msg = json.loads(data.decode())
                    self._handle_message(msg)
                except Exception:
                    pass
                finally:
                    conn.close()
        finally:
            srv.close()
            try:
                self._socket_path.unlink()
            except FileNotFoundError:
                pass
