#!/usr/bin/env python3
from __future__ import annotations
import os, json, socket, subprocess, tempfile, time
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget
from config import SPEEDS

class MpvPlayer:
    """Minimal mpv wrapper with IPC control and time-pos observer."""
    def __init__(self, parent_widget: QWidget, label: str):
        self.label = label
        self.widget = QWidget(parent_widget)
        self.widget.setAttribute(Qt.WA_NativeWindow, True)
        self.widget.setStyleSheet("background:black;")
        self.sock_path = os.path.join(
            tempfile.gettempdir(),
            f"mpv_{label}_{os.getpid()}_{int(time.time()*1000)}.sock"
        )
        self.proc = None
        self.sock = None
        self._buf = b""
        self.speed_idx = 2  # 1.0x default
        self.current_time = 0.0

    def start(self, filepath: str):
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass

        wid = int(self.widget.winId())
        cmd = [
            "mpv", filepath,
            f"--wid={wid}",
            "--no-config",
            "--keep-open=yes",
            "--osd-level=3",
            "--osd-font-size=24",
            "--pause",
            f"--input-ipc-server={self.sock_path}",
            "--cache=yes",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for _ in range(100):
            if os.path.exists(self.sock_path):
                break
            time.sleep(0.02)

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        for _ in range(100):
            try:
                self.sock.connect(self.sock_path)
                self.sock.setblocking(False)
                self._send_command(["observe_property", 1, "time-pos"])
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)

    def _send_command(self, command_list):
        if not self.sock:
            return
        data = (json.dumps({"command": command_list}) + "\n").encode("utf-8")
        try:
            self.sock.sendall(data)
        except (BrokenPipeError, OSError):
            pass

    def poll_events(self):
        if not self.sock:
            return
        try:
            data = self.sock.recv(4096)
            if not data:
                return
            self._buf += data
            lines = self._buf.split(b"\n")
            self._buf = lines[-1]
            for line in lines[:-1]:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                    if obj.get("event") == "property-change" and obj.get("name") == "time-pos":
                        d = obj.get("data")
                        if d is not None:
                            self.current_time = float(d)
                except (ValueError, json.JSONDecodeError):
                    pass
        except (BlockingIOError, OSError):
            pass

    def get_current_time(self) -> float:
        return self.current_time

    def set_speed(self, speed_idx: int):
        self.speed_idx = max(0, min(len(SPEEDS) - 1, speed_idx))
        self._send_command(["set_property", "speed", SPEEDS[self.speed_idx]])

    def seek(self, seconds: float, relative: bool = True):
        mode = "relative" if relative else "absolute"
        self._send_command(["seek", seconds, mode])

    def close(self):
        if self.sock:
            try:
                self._send_command(["quit"])
                self.sock.close()
            except Exception:
                pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass