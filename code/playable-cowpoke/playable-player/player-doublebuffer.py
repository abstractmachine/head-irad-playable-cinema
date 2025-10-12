#!/usr/bin/env python3
# Double-buffer video switcher using two embedded mpv processes controlled via JSON IPC.
# Robust IPC: request_id matching, persistent read buffer, retries, and --idle=yes.
import os, sys, csv, random, json, socket, subprocess, tempfile, time
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QGuiApplication

# --- Paths / config ---
PROJECT_ROOT = "/media/playback/PLAYABLE-D/project"
CSV_PATH     = os.path.join(PROJECT_ROOT, "metadata", "cinematheque.csv")
MOVIES_DIR   = os.path.join(PROJECT_ROOT, "movies")

# --- Behavior ---
PLAY_INTERVAL_MS = 3000
MIN_START_MS     = 2 * 60 * 1000
END_BUFFER_MS    = 2 * 60 * 1000
SAFETY_MS        = 1000

# Use X11/XWayland on the Pi
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

# ---------- helpers ----------
def load_filenames(csv_path):
    if not os.path.exists(csv_path):
        raise SystemExit(f"[!] CSV not found: {csv_path}")
    files = []
    with open(csv_path, newline="") as f:
        f.seek(0); dr = csv.DictReader(f)
        use_dict = dr.fieldnames and "filename" in dr.fieldnames
        f.seek(0)
        if use_dict:
            for row in csv.DictReader(f):
                fn = (row.get("filename") or "").strip()
                if fn and not fn.startswith("#"): files.append(fn)
        else:
            for row in csv.reader(f):
                if row and row[0] and not str(row[0]).startswith("#"):
                    files.append(row[0].strip())
    if not files:
        raise SystemExit("[!] No filenames loaded from CSV")
    return files

def random_start(duration_ms:int)->int:
    if duration_ms <= 0: return 0
    latest = max(0, duration_ms - END_BUFFER_MS - SAFETY_MS)
    if latest <= 0: return 0
    if latest <= MIN_START_MS: return latest
    return random.randint(MIN_START_MS, latest)

# ---------- UI host for embedded mpv windows ----------
class VideoPane(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setStyleSheet("background:black;")
        self.show()  # ensure it’s mapped

# ---------- mpv process wrapper ----------
class MpvProc:
    """An mpv process embedded via --wid and controlled over JSON IPC."""
    def __init__(self, host_widget: QWidget, label: str):
        self.label = label
        self.win = VideoPane(host_widget)
        self.ready = False
        self.target_ms = 0
        self.current_file = None

        # UNIX socket for JSON IPC
        self.sock_path = os.path.join(tempfile.gettempdir(), f"mpv_{label}_{os.getpid()}.sock")
        try: os.unlink(self.sock_path)
        except OSError: pass
        self.proc = None
        self.sock = None
        self._buf = b""
        self._req_id = 1  # monotonically increasing id

    def start(self):
        wid = int(self.win.winId())

        # Force numeric C locale for mpv (avoids libmpv locale crash)
        env = os.environ.copy()
        env["LC_NUMERIC"] = "C"

        cmd = [
            "mpv",
            f"--wid={wid}",
            "--no-config",
            "--idle=yes",                  # IPC comes up immediately; no race
            "--vo=gpu", "--gpu-context=x11egl",
            "--hwdec=auto-safe",
            "--keep-open=yes",
            "--osc=no", "--terminal=no",
            f"--input-ipc-server={self.sock_path}",
            "--mute=yes",
            "--volume=100",
            "--cache=yes", "--cache-secs=5", "--demuxer-max-back-bytes=50M",
        ]

        # For debugging, remove stdout/stderr redirection to see mpv logs.
        self.proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for the socket file to appear
        deadline = time.time() + 5.0
        while time.time() < deadline and not os.path.exists(self.sock_path):
            time.sleep(0.02)
        if not os.path.exists(self.sock_path):
            raise RuntimeError(f"[{self.label}] mpv IPC socket not created")

        # Connect with retries (mpv may create socket but start listening a bit later)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.time() + 5.0
        last_err = None
        while time.time() < deadline:
            try:
                self.sock.connect(self.sock_path)
                self.sock.setblocking(False)
                break
            except (ConnectionRefusedError, OSError) as e:
                last_err = e
                time.sleep(0.05)
        else:
            raise RuntimeError(f"[{self.label}] mpv IPC connect failed: {last_err}")

    # ---- IPC helpers ----
    def _send(self, obj):
        if not self.sock: return
        data = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            self.sock.sendall(data)
        except (BrokenPipeError, OSError):
            pass

    def _drain_until(self, predicate, timeout=0.5):
        """Read lines until predicate(msg) returns True; return that msg or None."""
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
                # else: ignore unsolicited events (property-change, log-message, etc.)
        return None

    def request(self, command_list, timeout=0.5):
        """Send a command with a unique request_id and wait for the matching reply."""
        rid = self._req_id; self._req_id += 1
        self._send({"command": command_list, "request_id": rid})

        def match(msg):
            return isinstance(msg, dict) and msg.get("request_id") == rid
        reply = self._drain_until(match, timeout=timeout)
        return reply

    def set_property(self, name, value):
        self.request(["set_property", name, value], timeout=0.3)

    def get_property(self, name):
        reply = self.request(["get_property", name], timeout=0.4)
        if not reply or reply.get("error") != "success":
            return None
        return reply.get("data", None)

    def command(self, *args):
        self.request(list(args), timeout=0.3)

    # ---- playback control ----
    def load_file(self, path: str):
        self.ready = False
        self.target_ms = 0
        self.current_file = path
        print(f"[prep] {self.label} <- {os.path.basename(path)}")
        self.command("loadfile", path, "replace")
        self.set_property("pause", False)
        self.set_property("mute", True)
        QTimer.singleShot(30, self._prepare_seek)

    def _prepare_seek(self):
        dur = self.get_property("duration")  # seconds (float) or None
        if not dur:
            QTimer.singleShot(40, self._prepare_seek)
            return
        try:
            dur_ms = int(float(dur) * 1000)
        except Exception:
            # If weird type slips through, retry
            QTimer.singleShot(40, self._prepare_seek)
            return

        self.target_ms = random_start(dur_ms)
        # precise absolute seek
        self.command("seek", self.target_ms / 1000.0, "absolute", "exact")
        self.set_property("pause", False)
        # poll until we've passed (or are close to) the target
        QTimer.singleShot(120, self._poll_ready)

    def _poll_ready(self):
        pos = self.get_property("time-pos")  # seconds float
        if pos is None:
            QTimer.singleShot(50, self._poll_ready); return
        try:
            pos_ms = int(float(pos) * 1000)
        except Exception:
            QTimer.singleShot(50, self._poll_ready); return

        if pos_ms + 150 >= self.target_ms:
            self.ready = True
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
                try: self._send({"command": ["quit"]})
                except Exception: pass
                self.sock.close()
        except Exception:
            pass
        try:
            if self.proc:
                self.proc.terminate()
                try: self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        try:
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
        except Exception:
            pass

# ---------- main window ----------
class DoubleBufferMPV(QMainWindow):
    def __init__(self, files):
        super().__init__()
        self.files = files[:]

        self.setStyleSheet("background:black;")
        self.showFullScreen()
        rect = QGuiApplication.primaryScreen().geometry()

        # Root container to keep both panes on same screen
        self.root = QWidget(self); self.setCentralWidget(self.root)
        self.root.setGeometry(rect); self.root.setStyleSheet("background:black;")

        # Start two embedded mpv processes
        self.a = MpvProc(self.root, "A")
        self.b = MpvProc(self.root, "B")
        self.a.win.setGeometry(rect); self.b.win.setGeometry(rect)
        self.a.start(); self.b.start()

        self.a.win.raise_()
        self.current, self.preload = self.a, self.b

        # Switch timer
        self.timer = QTimer(self); self.timer.setInterval(PLAY_INTERVAL_MS)
        self.timer.timeout.connect(self._switch_if_ready)

        # Begin
        self._prepare(self.current)
        self._wait_then_start_first()
        self._prepare(self.preload)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Q, Qt.Key_Space, Qt.Key_Escape):
            self.close()

    def closeEvent(self, e):
        self.a.close(); self.b.close()
        super().closeEvent(e)

    def _pick_file(self):
        return os.path.join(MOVIES_DIR, random.choice(self.files))

    def _prepare(self, slot: MpvProc):
        slot.load_file(self._pick_file())

    def _wait_then_start_first(self):
        if self.current.ready:
            self.current.go_live()
            self.timer.start()
        else:
            QTimer.singleShot(50, self._wait_then_start_first)

    def _switch_if_ready(self):
        if not self.preload.ready:
            return
        self.preload.go_live()
        self.current.go_hidden()
        self.current, self.preload = self.preload, self.current
        self.timer.stop(); self.timer.start(PLAY_INTERVAL_MS)
        self._prepare(self.preload)

# ---------- entry ----------
def main():
    files = load_filenames(CSV_PATH)
    app = QApplication(sys.argv)
    app.setOverrideCursor(Qt.BlankCursor)
    w = DoubleBufferMPV(files)
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()