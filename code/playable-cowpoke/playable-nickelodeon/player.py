#!/usr/bin/env python3
import os
import subprocess
import socket
import json
import time
import tempfile
import random
from PyQt5.QtWidgets import QMainWindow, QWidget
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QGuiApplication

from cinematheque import get_movies, fullpath
from switcher import gremlin_get, gremlin_tick

# --- Behavior ---
MIN_START_MS   = 2 * 60 * 1000
END_BUFFER_MS  = 2 * 60 * 1000
SAFETY_MS      = 1000

def random_start(duration_ms: int) -> int:
    """Pick a safe random start time avoiding first/last 2 minutes."""
    if duration_ms <= 0:
        return 0
    latest = max(0, duration_ms - END_BUFFER_MS - SAFETY_MS)
    if latest <= 0:
        return 0
    if latest <= MIN_START_MS:
        return latest
    return random.randint(MIN_START_MS, latest)

# --- UI host for embedded mpv ---
class VideoPane(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setStyleSheet("background:black;")
        self.setFocusPolicy(Qt.NoFocus)  # Don't accept keyboard focus
        self.show()

# --- mpv process wrapper ---
class MpvProc:
    """An mpv process embedded via --wid and controlled over JSON IPC."""
    
    def __init__(self, host_widget: QWidget, label: str):
        self.label = label
        self.win = VideoPane(host_widget)
        self.ready = False
        self.target_ms = None
        self.current_file = None
        
        self.sock_path = self._make_sock_path()
        self.proc = None
        self.sock = None
        self._buf = b""
        self._req_id = 1
        self._ipc_broken = False
    
    def _make_sock_path(self):
        return os.path.join(
            tempfile.gettempdir(),
            f"mpv_{self.label}_{os.getpid()}_{int(time.time()*1000)}.sock"
        )
    
    def start(self):
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass
        
        wid = int(self.win.winId())
        env = os.environ.copy()
        env["LC_NUMERIC"] = "C"
        
        cmd = [
            "mpv",
            f"--wid={wid}",
            "--no-config",
            "--idle=yes",
            "--vo=gpu",
            "--gpu-context=x11egl",
            "--hwdec=auto-safe",
            "--keep-open=yes",
            "--osc=no",
            "--osd-level=0",
            "--cursor-autohide=always",
            "--no-input-default-bindings",
            "--input-vo-keyboard=no",
            "--no-input-cursor",
            "--terminal=no",
            f"--input-ipc-server={self.sock_path}",
            "--mute=yes",
            "--volume=100",
            "--cache=yes",
            "--cache-secs=5",
            "--demuxer-max-back-bytes=50M",
        ]
        
        self.proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        # Wait for socket
        deadline = time.time() + 5.0
        while time.time() < deadline and not os.path.exists(self.sock_path):
            time.sleep(0.02)
        
        if not os.path.exists(self.sock_path):
            raise RuntimeError(f"[{self.label}] mpv socket not created")
        
        # Connect
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                self.sock.connect(self.sock_path)
                self.sock.setblocking(False)
                self._ipc_broken = False
                self._buf = b""
                print(f"[{self.label}] mpv started")
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        
        raise RuntimeError(f"[{self.label}] mpv IPC connect failed")
    
    def _send(self, obj):
        if not self.sock or self._ipc_broken:
            return
        data = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            self.sock.sendall(data)
        except (BrokenPipeError, OSError):
            self._ipc_broken = True
    
    def _drain_until(self, predicate, timeout=0.5):
        if not self.sock or self._ipc_broken:
            return None
        end = time.time() + timeout
        while time.time() < end:
            try:
                chunk = self.sock.recv(4096)
                if chunk:
                    self._buf += chunk
                else:
                    time.sleep(0.01)
            except BlockingIOError:
                time.sleep(0.01)
            except OSError:
                self._ipc_broken = True
                return None
            
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "ignore"))
                except Exception:
                    continue
                if predicate(msg):
                    return msg
        return None
    
    def request(self, command_list, timeout=0.5):
        if self._ipc_broken:
            return None
        rid = self._req_id
        self._req_id += 1
        self._send({"command": command_list, "request_id": rid})
        
        def match(msg):
            return isinstance(msg, dict) and msg.get("request_id") == rid
        
        return self._drain_until(match, timeout=timeout)
    
    def set_property(self, name, value):
        self.request(["set_property", name, value], timeout=0.3)
    
    def get_property(self, name):
        if self._ipc_broken:
            return None
        reply = self.request(["get_property", name], timeout=0.4)
        if not reply or reply.get("error") != "success":
            return None
        return reply.get("data")
    
    def command(self, *args):
        self.request(list(args), timeout=0.3)
    
    def load_file(self, path: str, start_ms: int = None):
        self.ready = False
        self.current_file = path
        self.target_ms = start_ms
        print(f"[{self.label}] load {os.path.basename(path)} @ {start_ms}ms")
        self.command("loadfile", path, "replace")
        self.set_property("pause", False)
        self.set_property("mute", True)
        QTimer.singleShot(30, self._prepare_seek)
    
    def _prepare_seek(self):
        if self._ipc_broken:
            return
        
        dur = self.get_property("duration")
        if not dur:
            QTimer.singleShot(40, self._prepare_seek)
            return
        
        try:
            dur_ms = int(float(dur) * 1000)
        except Exception:
            QTimer.singleShot(40, self._prepare_seek)
            return
        
        # Use provided target_ms or pick random safe start
        if self.target_ms is not None and self.target_ms >= 0:
            seek_ms = self.target_ms
        else:
            seek_ms = random_start(dur_ms)
            self.target_ms = seek_ms
        
        self.command("seek", seek_ms / 1000.0, "absolute", "exact")
        self.set_property("pause", False)
        QTimer.singleShot(120, self._poll_ready)
    
    def _poll_ready(self):
        if self._ipc_broken:
            return
        
        pos = self.get_property("time-pos")
        if pos is None:
            QTimer.singleShot(50, self._poll_ready)
            return
        
        try:
            pos_ms = int(float(pos) * 1000)
        except Exception:
            QTimer.singleShot(50, self._poll_ready)
            return
        
        if pos_ms + 150 >= self.target_ms:
            self.ready = True
            print(f"[{self.label}] ready @ {pos_ms}ms")
            return
        
        QTimer.singleShot(50, self._poll_ready)
    
    def go_live(self):
        self.win.raise_()
        self.set_property("mute", False)
        self.set_property("pause", False)
    
    def go_hidden(self):
        self.set_property("mute", True)
        self.set_property("pause", False)
    
    def close(self):
        try:
            if self.sock:
                try:
                    self._send({"command": ["quit"]})
                except Exception:
                    pass
                self.sock.close()
        except Exception:
            pass
        
        try:
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        
        try:
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
        except Exception:
            pass


# --- Main window with double-buffer ---
class DoubleBufferMPV(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.setStyleSheet("background:black;")
        self.setFocusPolicy(Qt.StrongFocus)  # Accept keyboard focus
        self.showFullScreen()
        rect = QGuiApplication.primaryScreen().geometry()
        
        self.root = QWidget(self)
        self.setCentralWidget(self.root)
        self.root.setGeometry(rect)
        self.root.setStyleSheet("background:black;")
        self.root.setFocusPolicy(Qt.NoFocus)
        
        # Create two mpv instances
        self.a = MpvProc(self.root, "A")
        self.b = MpvProc(self.root, "B")
        self.a.win.setGeometry(rect)
        self.b.win.setGeometry(rect)
        
        self.a.start()
        self.b.start()
        
        self.a.win.raise_()
        self.current = self.a
        self.preload = self.b
        
        # Gremlin timer
        self.gremlin_timer = QTimer(self)
        self.gremlin_timer.timeout.connect(self._gremlin_tick)
        
        print("[player] double-buffer initialized")
    
    def play(self, filepath: str, start_ms: int = 0):
        """Load into preload slot and switch when ready."""
        self.preload.load_file(filepath, start_ms)
        QTimer.singleShot(200, self._wait_and_switch)
    
    def _wait_and_switch(self):
        if self.preload.ready:
            self.preload.go_live()
            self.current.go_hidden()
            self.current, self.preload = self.preload, self.current
            print(f"[player] switched → {self.current.label} live")
        else:
            QTimer.singleShot(50, self._wait_and_switch)
    
    def set_gremlin(self, enabled: bool):
        """Enable/disable gremlin auto-switching."""
        if enabled:
            _, interval_s = gremlin_get()
            self.gremlin_timer.start(interval_s * 1000)
        else:
            self.gremlin_timer.stop()
    
    def _gremlin_tick(self):
        """Check if gremlin wants to switch."""
        sel = gremlin_tick()
        if sel:
            idx, title, start_ms = sel
            if idx >= 0 and title:
                print(f"[switcher] GREMLIN → index={idx} title={title} start={start_ms}ms")
                self.play(fullpath(title), start_ms)
    
    def closeEvent(self, event):
        self.a.close()
        self.b.close()
        super().closeEvent(event)