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
        self.duration = 0.0
        self.auto_pause_at = None  # For movie: pause at specific end time
        self.enable_looping = False  # For gameplay looping

    def start(self, filepath: str, loop: bool = False):
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass

        self.enable_looping = loop
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
        if loop:
            cmd.append("--loop=inf")
        
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
                self._send_command(["observe_property", 2, "duration"])
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
                    if obj.get("event") == "property-change":
                        name = obj.get("name")
                        d = obj.get("data")
                        if name == "time-pos" and d is not None:
                            self.current_time = float(d)
                            # Check for auto-pause - stop exactly at end time
                            if self.auto_pause_at is not None and self.current_time >= self.auto_pause_at:
                                self.pause()
                                # Seek back to exact end time
                                self.seek(self.auto_pause_at, relative=False)
                                self.auto_pause_at = None
                        elif name == "duration" and d is not None:
                            self.duration = float(d)
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

    def seek_to_shot(self, start_seconds: float, end_seconds: float):
        """Seek to start time and set auto-pause at end time."""
        self.seek(start_seconds, relative=False)
        self.auto_pause_at = end_seconds
        self.play()

    def play(self):
        self._send_command(["set_property", "pause", False])

    def pause(self):
        self._send_command(["set_property", "pause", True])

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