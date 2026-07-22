#!/usr/bin/env python3
"""Sync Visualizer — patching workspace for live video sync tools.

Launched via:
    crossing visualizer sync

Keyboard shortcuts:
    h             — toggle right-side panel + node chrome (presentation mode)
    f             — toggle fullscreen on the current monitor (edge-to-edge, no OS chrome)
    Escape / Ctrl+Q / Ctrl+W — close

Layout:
  LEFT   — central workspace (drop area for nodes)
  RIGHT  — palette panel with draggable input blocks

State is persisted to prefs across sessions:
  - window geometry and fullscreen state
  - panel visibility
  - all dropped node positions, sizes, and device strings
"""

from __future__ import annotations

import os
import select as _select_mod
import subprocess as _subprocess
import sys
import threading as _threading
import time as _time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme

from PyQt5.QtCore import (
    QEvent,
    QMimeData,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QDrag,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygon,
)
from PyQt5.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt5.QtSvg import QSvgRenderer
    _HAS_SVG = True
except ImportError:
    _HAS_SVG = False

try:
    import cv2
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False

if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WINDOW_TITLE    = "Sync Visualizer"
_WINDOW_W        = 1100
_WINDOW_H        = 700
_PANEL_W         = 220

_WORKSPACE_BG    = "#808080"   # 50% gray workspace canvas
_NODE_TITLE_BG   = "#666666"   # 40% gray node title bar
_NODE_BODY_BG    = "#404040"   # 25% gray node content area

_NODE_TITLE_H    = 26
_NODE_DEFAULT_W  = 480
_NODE_DEFAULT_H  = _NODE_TITLE_H + round(_NODE_DEFAULT_W * 9 / 16)   # 296
_NODE_MIN_W      = 240
_NODE_MIN_H      = _NODE_TITLE_H + round(_NODE_MIN_W * 9 / 16)

_HANDLE_SIZE     = 14   # px — corner resize hit zone
_EDGE_THICKNESS  = 6    # px — edge resize hit zone for FrameVectorNode
_FV_MIN_H        = _NODE_TITLE_H + 80   # minimum FrameVectorNode height
_STAR_COLOR_A    = QColor("#00ff00")    # inner-edge colour (low radius)
_STAR_COLOR_B    = QColor("#ff0000")    # outer-edge colour (high radius)
_NODE_TEXT_COLOR = "#ffffff"            # text colour inside module windows
_PEN_WIDTH       = 2

_VIDEO_DEVICE        = "/dev/video0"   # Elgato Cam Link 4K
_VIDEO_WIDTH         = 1920
_VIDEO_HEIGHT        = 1080
_VIDEO_FPS           = 30             # 30fps MJPEG ≈ 10 MB/s; CLIP runs at 4–10 Hz so 60fps is wasted work
_VIDEO_INPUT_FORMAT  = "mjpeg"        # prefer MJPEG; set "" to let ffmpeg auto-negotiate
_VIDEO_RECONNECT_S   = 2    # seconds between reconnect probe attempts

_MIME_TYPE       = "application/x-crossing-sync-palette-item"

# Prefs keys
_PREFS_GEOM      = "sync_visualizer_geometry"       # [x, y, w, h]
_PREFS_FULLSCR   = "sync_visualizer_fullscreen"     # bool
_PREFS_PANEL     = "sync_visualizer_panel_visible"  # bool
_PREFS_NODES     = "sync_visualizer_nodes"          # list of dicts
_PREFS_CONNS     = "sync_visualizer_connections"    # list of dicts
_PREFS_WORKSPACE_BG = "sync_visualizer_workspace_bg"   # hex string
_PREFS_NODE_BG      = "sync_visualizer_node_bg"        # hex string (with optional alpha)
_PREFS_NODE_TEXT    = "sync_visualizer_node_text"      # hex string
_PREFS_STAR_A       = "sync_visualizer_star_color_a"   # hex string
_PREFS_STAR_B       = "sync_visualizer_star_color_b"   # hex string

# Port geometry
_PORT_SIZE       = 12   # px — triangle width/height
_PORT_COLOR_IDLE   = QColor(_NODE_BODY_BG)   # same as node content background
_PORT_COLOR_ACTIVE = QColor("#ff00ff")        # fuchsia while dragging / hover target
_PORT_COLOR_CONN   = QColor(_NODE_BODY_BG)   # same as node content background
_CABLE_COLOR_DRAG  = QColor("#ff00ff")
_CABLE_COLOR_CONN  = QColor(_NODE_BODY_BG)   # same as node content background
_CABLE_WIDTH       = 2

# Title-bar button styles — shared across all nodes
_TB_ICON_BTN_SS = (
    "QPushButton {"
    "  background: transparent; border: none; padding: 0;"
    "}"
    "QPushButton:hover {"
    "  background: #ff00ff; border-radius: 3px;"
    "}"
)
_TB_TEXT_BTN_SS = (
    "QPushButton {"
    "  background: transparent; color: #ffffff;"
    "  border: none; padding: 0 4px;"
    "}"
    "QPushButton:hover {"
    "  background: #ff00ff; border-radius: 3px; color: #ffffff;"
    "}"
)
# Close button has an extra font-size rule
_TB_CLOSE_BTN_SS = (
    "QPushButton {"
    "  background: transparent; color: #ffffff;"
    "  border: none; font-size: 14px; padding: 0;"
    "}"
    "QPushButton:hover {"
    "  background: #ff00ff; border-radius: 3px; color: #ffffff;"
    "}"
)

# Node ID counter (session-scoped)
_next_node_id: list[int] = [1]   # mutable singleton


def _new_node_id() -> int:
    nid = _next_node_id[0]
    _next_node_id[0] += 1
    return nid


# ---------------------------------------------------------------------------
# SVG icon helper
# ---------------------------------------------------------------------------

def _svg_icon(name: str, size: int = 16, color: str = "#ffffff") -> QIcon:
    """Load an iconoir SVG, recolour strokes/fills to *color*, return QIcon.
    All modes/states use the same pixmap so Qt never auto-tints it.
    """
    icon_dir = Path(__file__).parent.parent / "styles" / "icons" / "iconoir"
    path = icon_dir / f"{name}.svg"
    if not path.exists():
        return QIcon()
    raw = path.read_bytes()
    coloured = raw.replace(b"#000000", color.encode())
    if _HAS_SVG:
        renderer = QSvgRenderer(coloured)
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        renderer.render(painter)
        painter.end()
        icon = QIcon()
        for mode in (QIcon.Normal, QIcon.Active, QIcon.Selected, QIcon.Disabled):
            icon.addPixmap(pix, mode, QIcon.Off)
            icon.addPixmap(pix, mode, QIcon.On)
        return icon
    return QIcon()


class _TbBtn(QPushButton):
    """Title-bar button that manually swaps its icon on enter/leave
    so hover-fuchsia is driven by mouse proximity, not Qt focus state.
    Call set_icon_name() once after construction.
    """

    def __init__(self, icon_name: str = "", text: str = "",
                 icon_size: int = 14, parent=None) -> None:
        super().__init__(text, parent)
        self._icon_name  = icon_name
        self._icon_size  = icon_size
        self._icon_color = "#ffffff"   # updated by set_icon_color()
        self.setFlat(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._refresh_icon(self._icon_color)

    def set_icon_color(self, color: str) -> None:
        self._icon_color = color
        self._refresh_icon(color)

    def set_icon(self, name: str, color: str | None = None) -> None:
        self._icon_name = name
        if color is not None:
            self._icon_color = color
        self._refresh_icon(self._icon_color)

    def _refresh_icon(self, color: str) -> None:
        if self._icon_name:
            self.setIcon(_svg_icon(self._icon_name, self._icon_size, color))
            self.setIconSize(QSize(self._icon_size, self._icon_size))

    def enterEvent(self, event) -> None:
        self._refresh_icon("#ffffff")
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._refresh_icon(self._icon_color)
        super().leaveEvent(event)


def _enumerate_video_devices() -> list:
    """Return sorted list of /dev/videoN paths present on the system."""
    import glob as _glob
    import re as _re
    return sorted(
        d for d in _glob.glob("/dev/video*")
        if _re.match(r"/dev/video\d+$", d)
    )


def _find_alsa_source_for_video(video_device: str) -> str | None:
    """Best-effort: return the PulseAudio source name for a V4L2 device.

    Matches by the USB serial string embedded in both the udevadm output and
    the ALSA source name (e.g. alsa_input.usb-VendorModel-00.analog-stereo).
    Falls back to the first USB alsa_input found.
    """
    if not video_device:
        return None
    import subprocess as _sp
    try:
        info = _sp.check_output(
            ["udevadm", "info", "--query=all", f"--name={video_device}"],
            text=True, stderr=_sp.DEVNULL,
        )
        serial = None
        for line in info.splitlines():
            if line.startswith("E: ID_SERIAL="):
                serial = line.split("=", 1)[1].strip().replace(" ", "_")
                break
        sources_out = _sp.check_output(
            ["pactl", "list", "sources", "short"],
            text=True, stderr=_sp.DEVNULL,
        )
        fallback = None
        for line in sources_out.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1]
            if not name.startswith("alsa_input"):
                continue
            if serial and serial in name:
                return name
            if "usb" in name.lower() and fallback is None:
                fallback = name
        return fallback
    except Exception:
        return None


def _cleanup_audio_loopbacks() -> None:
    """Unload ALL module-loopback instances (any process, any session).

    Calling ``pactl unload-module module-loopback`` without an ID removes every
    running instance at once, which cleans up stale loops from crashed or
    previous runs of the Sync Visualizer.
    """
    import subprocess as _sp
    try:
        _sp.run(
            ["pactl", "unload-module", "module-loopback"],
            check=False, capture_output=True,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# _ResizeHandle — bottom-corner drag widget
# ---------------------------------------------------------------------------

class _ResizeHandle(QWidget):
    """Tiny corner widget that emits resize drag events.

    Emits:
      resize_started(corner)            — on mouse press
      resize_dragged(corner, delta_x)   — on mouse move (delta from press)
      resize_released(corner)           — on mouse release
    """

    resize_started  = pyqtSignal(str)       # corner: "bl" | "br"
    resize_dragged  = pyqtSignal(str, int)  # corner, delta_x from press origin
    resize_released = pyqtSignal(str)

    def __init__(self, corner: str, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._corner  = corner
        self._press_x: int | None = None
        self.setFixedSize(_HANDLE_SIZE, _HANDLE_SIZE)
        self.setCursor(
            Qt.SizeFDiagCursor if corner == "br" else Qt.SizeBDiagCursor
        )
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_x = event.globalPos().x()
            self.resize_started.emit(self._corner)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton and self._press_x is not None:
            self.resize_dragged.emit(self._corner,
                                     event.globalPos().x() - self._press_x)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._press_x is not None:
            self.resize_released.emit(self._corner)
            self._press_x = None
        super().mouseReleaseEvent(event)


class _EdgeResizeHandle(QWidget):
    """Thin transparent strip along one node edge for free (non-aspect-locked) resize.

    Emits:
      resize_started(edge)                — on mouse press
      resize_dragged(edge, delta_x, delta_y) — on mouse move (delta from press)
      resize_released(edge)               — on mouse release
    """

    resize_started  = pyqtSignal(str)
    resize_dragged  = pyqtSignal(str, int, int)
    resize_released = pyqtSignal(str)

    def __init__(self, edge: str, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._edge      = edge
        self._press_pos = None
        if edge in ("top", "bottom"):
            cursor = Qt.SizeVerCursor
        elif edge in ("left", "right"):
            cursor = Qt.SizeHorCursor
        elif edge in ("tl", "br"):
            cursor = Qt.SizeFDiagCursor
        else:  # "tr", "bl"
            cursor = Qt.SizeBDiagCursor
        self.setCursor(cursor)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.globalPos()
            self.resize_started.emit(self._edge)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton and self._press_pos is not None:
            d = event.globalPos() - self._press_pos
            self.resize_dragged.emit(self._edge, d.x(), d.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._press_pos is not None:
            self.resize_released.emit(self._edge)
            self._press_pos = None
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# _VideoReaderThread — ffmpeg subprocess capture, off the main thread
# ---------------------------------------------------------------------------

class _VideoReaderSignals(QObject):
    """QObject signal carrier for _VideoReaderThread."""
    frame_ready      = pyqtSignal(object)   # np.ndarray HxWx3 uint8 RGB
    device_lost      = pyqtSignal()
    device_recovered = pyqtSignal()


class _VideoReaderThread:
    """Background daemon thread that captures from a V4L2 device via ffmpeg.

    Uses ffmpeg (subprocess) instead of OpenCV's native V4L2 backend.
    Rationale:
    - OpenCV's V4L2 backend can throw unrecoverable C++ exceptions after
      repeated select() timeouts, crashing the whole process.
    - ffmpeg runs in a child process: any crash is isolated; our thread
      just sees the pipe close and restarts.
    - select() on the pipe fd gives a clean 2-second timeout per read so
      a stalled device is detected quickly without blocking indefinitely.

    Uses Python threading.Thread(daemon=True) so the process exits cleanly
    even if a read is in progress at shutdown.
    Signals are delivered to the Qt main thread via queued connections.
    """

    _FRAME_SIZE = _VIDEO_WIDTH * _VIDEO_HEIGHT * 3   # RGB24 bytes per frame

    def __init__(self, device: str, parent: "QObject | None" = None) -> None:
        self._device  = device
        self._stopping = False
        self._signals = _VideoReaderSignals(parent)
        self._thread  = _threading.Thread(
            target=self._run, daemon=True, name="VideoReader")

    # ------------------------------------------------------------------
    # Public signal proxies (same interface as the old QThread version)
    # ------------------------------------------------------------------

    @property
    def frame_ready(self):
        return self._signals.frame_ready

    @property
    def device_lost(self):
        return self._signals.device_lost

    @property
    def device_recovered(self):
        return self._signals.device_recovered

    # ------------------------------------------------------------------
    # QThread-compatible control API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def isRunning(self) -> bool:
        return self._thread.is_alive()

    def quit(self) -> None:
        """No-op: provided for QThread-compatible callers."""

    def terminate(self) -> None:
        """No-op: daemon threads are abandoned on process exit."""

    def wait(self, msecs: "int | None" = None) -> bool:
        """Join the background thread; optional timeout in milliseconds."""
        timeout = (msecs / 1000.0) if msecs is not None else None
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def request_stop(self) -> None:
        self._stopping = True

    # ------------------------------------------------------------------
    # Thread body
    # ------------------------------------------------------------------

    def _run(self) -> None:
        ever_connected = False

        while not self._stopping:
            proc = self._launch_ffmpeg()
            if proc is None:
                # ffmpeg not installed or device path is wrong
                if not ever_connected:
                    self._signals.device_lost.emit()
                    ever_connected = True
                self._sleep_reconnect()
                continue

            got_frame = False
            buf = bytearray()
            fd  = proc.stdout.fileno()

            while not self._stopping:
                # 5-second deadline per read attempt.  After a forced
                # teardown (SIGKILL via fuser -k) the V4L2 driver needs
                # ~15-20 s to reset; short retries poll every 5 s and pick
                # up the signal as soon as the driver is ready, rather than
                # blocking for 30 s before the first retry.
                # Once streaming is established tighten to 3 s for stall
                # detection.
                timeout = 5.0 if not got_frame else 3.0
                try:
                    ready = _select_mod.select([proc.stdout], [], [], timeout)[0]
                except Exception:
                    break
                if not ready:
                    break   # stall — kill and restart ffmpeg

                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break   # ffmpeg exited

                buf.extend(chunk)

                # drain all complete frames from the buffer
                while len(buf) >= self._FRAME_SIZE:
                    raw   = bytes(buf[:self._FRAME_SIZE])
                    del buf[:self._FRAME_SIZE]
                    frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                        (_VIDEO_HEIGHT, _VIDEO_WIDTH, 3))
                    if not got_frame:
                        got_frame = True
                        if ever_connected:
                            self._signals.device_recovered.emit()
                        ever_connected = True
                    self._signals.frame_ready.emit(frame.copy())

            # ---- teardown ------------------------------------------------
            # SIGTERM first: lets ffmpeg call ioctl(VIDIOC_STREAMOFF) and
            # release DMA buffers cleanly.  If we SIGKILL immediately the
            # kernel has to do the cleanup itself, which can take 10-20 s
            # and blocks the next ffmpeg open from receiving frames.
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

            # Log any ffmpeg error output so failures are visible in console
            try:
                err = proc.stderr.read().decode(errors="replace").strip()
                if err:
                    import sys as _sys
                    print(f"[VideoReader] ffmpeg: {err}", file=_sys.stderr)
            except Exception:
                pass

            if self._stopping:
                return

            if ever_connected and got_frame:
                self._signals.device_lost.emit()
            elif not ever_connected:
                self._signals.device_lost.emit()
                ever_connected = True

            # Fast retry if we never got a frame this session (device still
            # resetting); normal delay after a real signal loss.
            if got_frame:
                self._sleep_reconnect()
            else:
                for _ in range(5):   # 0.5 s fast retry
                    if self._stopping:
                        return
                    _time.sleep(0.1)

    def _launch_ffmpeg(self) -> "_subprocess.Popen | None":
        """Start an ffmpeg process that writes raw RGB24 frames to stdout.

        No input format constraints: let ffmpeg and the V4L2 driver negotiate
        whatever the HDMI source is sending (the Cam Link 4K locks its output
        format to the incoming HDMI signal, so forcing a framerate or pixel
        format often causes an immediate failure).  A scale filter guarantees
        the output is always _VIDEO_WIDTH x _VIDEO_HEIGHT regardless of what
        the PS4 / HDMI source negotiated.
        """
        try:
            cmd = [
                "ffmpeg", "-loglevel", "warning",
                "-f", "v4l2",
                "-i", self._device,
                "-vf", f"scale={_VIDEO_WIDTH}:{_VIDEO_HEIGHT}",
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "pipe:1",
            ]
            return _subprocess.Popen(
                cmd,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.PIPE,   # captured; logged if ffmpeg exits early
                bufsize=0,
            )
        except (OSError, FileNotFoundError):
            return None

    def _sleep_reconnect(self) -> None:
        """Sleep _VIDEO_RECONNECT_S seconds in interruptible 100 ms steps."""
        for _ in range(_VIDEO_RECONNECT_S * 10):
            if self._stopping:
                return
            _time.sleep(0.1)


# ---------------------------------------------------------------------------
# LiveVideoWidget — low-latency /dev/video0 preview
# ---------------------------------------------------------------------------

class LiveVideoWidget(QLabel):
    """Live video preview driven by a background ffmpeg capture thread.

    Frames arrive via a signal from `_VideoReaderThread` so the main thread
    is never blocked by V4L2 I/O.  When the device disappears the widget
    shows a "reconnecting" message and automatically resumes once the device
    comes back.

    Falls back to a "No live video signal" message when ffmpeg is unavailable
    or the device cannot be opened initially.
    """

    def __init__(self, device: str = _VIDEO_DEVICE, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._device             = device
        self._thread: _VideoReaderThread | None = None
        self._last_pixmap: QPixmap | None = None
        self._last_frame_rgb     = None   # np.ndarray HxWx3 uint8, or None

        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(
            f"background: {theme.CANVAS_BG}; color: {theme.TEXT_DIM};"
        )
        self._try_open()

    def _try_open(self) -> None:
        self._thread = _VideoReaderThread(self._device, self)
        self._thread.frame_ready.connect(self._on_frame_ready)
        self._thread.device_lost.connect(self._on_device_lost)
        self._thread.device_recovered.connect(self._on_device_recovered)
        self._thread.start()

    def _on_frame_ready(self, frame_rgb) -> None:
        self._last_frame_rgb = frame_rgb
        h, w, ch = frame_rgb.shape
        img = QImage(frame_rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self._last_pixmap = QPixmap.fromImage(img)
        self._redisplay()

    def _on_device_lost(self) -> None:
        self._last_pixmap    = None
        self._last_frame_rgb = None
        self.setText(f"No signal on {self._device}\nReconnecting…")

    def _on_device_recovered(self) -> None:
        self.setText("")   # clear message; next frame_ready will paint over it

    def latest_frame_rgb(self):
        """Return the most recent captured frame as a HxWx3 uint8 numpy array.

        Returns None if no frame has been captured yet.
        Does NOT block waiting for a new frame.
        """
        return self._last_frame_rgb

    def _redisplay(self) -> None:
        if self._last_pixmap is None:
            return
        scaled = self._last_pixmap.scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.setPixmap(scaled)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._redisplay()

    def stop(self) -> None:
        if self._thread is not None:
            self._thread.request_stop()
            # Daemon thread: the process exits cleanly even if cap.read() is
            # still blocking.  Give it up to 3 s to finish voluntarily so the
            # V4L2 device is released before the widget is torn down, but do
            # not block indefinitely.
            self._thread.wait(3000)
            self._thread = None


# ---------------------------------------------------------------------------
# SyncNode — base class for workspace nodes
# ---------------------------------------------------------------------------

class SyncNode(QWidget):
    """Base class for nodes dropped onto SyncWorkspace.

    Provides:
    - Title bar (_NODE_TITLE_BG) with label + close button
    - Content area (_NODE_BODY_BG) for subclass widgets
    - Title-bar drag to move (via eventFilter)
    - Bottom-corner resize handles (_ResizeHandle), enforcing 16:9 content
    - set_chrome_visible() for presentation / hide mode
    """

    closed = pyqtSignal(object)

    def __init__(self, title: str, parent: QWidget = None) -> None:
        super().__init__(parent)

        self.node_id: int = _new_node_id()
        self._drag_global_start: QPoint | None = None
        self._drag_node_start:   QPoint | None = None
        self._resize_orig_geom                 = None   # QRect captured at drag start
        self._ports_visible: bool              = False  # shown on hover

        # ── Outer layout ──────────────────────────────────────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Title bar ─────────────────────────────────────────────────────
        self._title_bar = QWidget(self)
        self._title_bar.setFixedHeight(_NODE_TITLE_H)
        self._title_bar.setStyleSheet(f"background: {_NODE_TITLE_BG};")
        self._title_bar.setCursor(Qt.SizeAllCursor)

        tb = QHBoxLayout(self._title_bar)
        tb.setContentsMargins(6, 0, 4, 0)
        tb.setSpacing(0)
        self._tb_layout = tb

        self._title_label = QLabel(title, self._title_bar)
        self._title_label.setStyleSheet(
            "color: #ffffff; background: transparent;"
        )
        self._title_label.setFont(theme.font_ui())
        tb.addWidget(self._title_label, 0, Qt.AlignVCenter)
        tb.addStretch()

        self._close_btn = QPushButton("×", self._title_bar)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setFlat(True)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setFocusPolicy(Qt.NoFocus)
        self._close_btn.setStyleSheet(_TB_CLOSE_BTN_SS)
        self._close_btn.clicked.connect(self._on_close)
        tb.addWidget(self._close_btn, 0, Qt.AlignVCenter)
        outer.addWidget(self._title_bar)

        # ── Content area ──────────────────────────────────────────────────
        self._content = QWidget(self)
        self._content.setStyleSheet(f"background: {_NODE_BODY_BG};")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)
        outer.addWidget(self._content, 1)

        # ── Resize handles (children, not in layout) ──────────────────────
        self._handle_bl = _ResizeHandle("bl", self)
        self._handle_br = _ResizeHandle("br", self)
        for h in (self._handle_bl, self._handle_br):
            h.resize_started.connect(self._on_resize_start)
            h.resize_dragged.connect(self._on_resize_drag)
            h.resize_released.connect(self._on_resize_end)

        self._title_bar.installEventFilter(self)
        self._chrome_globally_visible = True  # False when h-key hides the panel

        # Set initial size (triggers resizeEvent → handle positioning)
        self.resize(_NODE_DEFAULT_W, _NODE_DEFAULT_H)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def content_layout(self) -> QVBoxLayout:
        return self._content_layout

    def node_type(self) -> str:
        return "base"

    def state_dict(self) -> dict:
        return {
            "type": self.node_type(),
            "node_id": self.node_id,
            "x": self.x(), "y": self.y(),
            "w": self.width(), "h": self.height(),
        }

    # ------------------------------------------------------------------
    # Port API — subclasses override to advertise ports
    # ------------------------------------------------------------------

    # Each spec entry:  {"type": "image"|"vector", "shape": "half_circle"|"triangle",
    #                    "dimension": int|None, "label": str|None}

    def output_specs(self) -> dict:
        """Return output port specs keyed by port name."""
        return {}

    def input_specs(self) -> dict:
        """Return input port specs keyed by port name."""
        return {}

    def output_ports(self) -> list[str]:
        """Derived from output_specs() — subclasses should not override this."""
        return list(self.output_specs().keys())

    def input_ports(self) -> list[str]:
        """Derived from input_specs() — subclasses should not override this."""
        return list(self.input_specs().keys())

    # ------------------------------------------------------------------
    # Event-driven dispatch API
    # ------------------------------------------------------------------

    def emit_output(self, port_name: str, value, meta: dict | None = None) -> None:
        """Push *value* through output port *port_name* to all connected nodes."""
        ws = self.parent()
        if isinstance(ws, SyncWorkspace):
            ws.dispatch_output(self, port_name, value, meta or {})

    def receive_input(self, port_name: str, value, meta: dict | None = None) -> None:
        """Called by SyncWorkspace.dispatch_output when data arrives on an input port.
        Subclasses override this to handle incoming values."""
        pass

    def output_port_pos(self, port: str) -> QPoint:
        """Return the port TIP in parent (workspace) coordinates.
        The triangle base sits at the node right edge; the tip extends outward."""
        return self.mapToParent(QPoint(self.width() + _PORT_SIZE, self.height() // 2))

    def input_port_pos(self, port: str) -> QPoint:
        """Return the port TIP in parent (workspace) coordinates.
        The triangle base sits at the node left edge; the tip extends outward."""
        return self.mapToParent(QPoint(-_PORT_SIZE, self.height() // 2))

    def set_ports_visible(self, visible: bool) -> None:
        if self._ports_visible != visible:
            self._ports_visible = visible
            self.update()

    def _port_connected(self, port: str, is_output: bool) -> bool:
        """Ask the workspace whether this port has a live connection."""
        ws = self.parent()
        if isinstance(ws, SyncWorkspace):
            return ws.is_port_connected(self.node_id, port, is_output)
        return False

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        # Port triangles are drawn by SyncWorkspace so they live outside node
        # bounds and receive workspace-level mouse events correctly.

    def set_chrome_visible(self, visible: bool) -> None:
        """Set chrome visibility driven by the h-key panel toggle.

        When visible (panel shown): title bar always displayed.
        When hidden (panel hidden): title bar hidden, revealed only on hover.
        """
        self._chrome_globally_visible = visible
        self._apply_title_chrome(visible)
        self._handle_bl.setVisible(visible)
        self._handle_br.setVisible(visible)

    def _apply_title_chrome(self, show: bool) -> None:
        """Show or hide the title bar chrome (bg, label, close button).

        Iterates every widget in the title-bar layout so subclass buttons
        are hidden automatically — no per-subclass override needed for
        hide/show logic.
        """
        if show:
            self._title_bar.setStyleSheet(f"background: {_NODE_TITLE_BG};")
        else:
            self._title_bar.setStyleSheet("background: transparent;")
        for i in range(self._tb_layout.count()):
            item = self._tb_layout.itemAt(i)
            if item and item.widget():
                item.widget().setVisible(show)

    # ------------------------------------------------------------------
    # Hover reveals title bar only in h-key hidden mode
    # ------------------------------------------------------------------

    def enterEvent(self, event) -> None:
        if not self._chrome_globally_visible:
            self._apply_title_chrome(True)
        self.set_ports_visible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._chrome_globally_visible and self._drag_global_start is None:
            self._apply_title_chrome(False)
        self.set_ports_visible(False)
        super().leaveEvent(event)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        self.closed.emit(self)
        self.hide()
        self.deleteLater()

    # ------------------------------------------------------------------
    # Resize handles — maintain 16:9
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_handles()
        # Cables and port triangles are painted by the workspace — repaint it.
        if isinstance(self.parent(), SyncWorkspace):
            self.parent().update()

    def _reposition_handles(self) -> None:
        s = _HANDLE_SIZE
        w = self.width()
        h = self.height()
        self._handle_bl.move(0, h - s)
        self._handle_br.move(w - s, h - s)
        self._handle_bl.raise_()
        self._handle_br.raise_()

    def _on_resize_start(self, corner: str) -> None:
        self._resize_orig_geom = self.geometry()

    def _on_resize_drag(self, corner: str, delta_x: int) -> None:
        orig = self._resize_orig_geom
        if orig is None:
            return
        if corner == "br":
            new_w = max(_NODE_MIN_W, orig.width() + delta_x)
            new_h = _NODE_TITLE_H + round(new_w * 9 / 16)
            self.setGeometry(orig.left(), orig.top(), new_w, new_h)
        else:   # "bl" — keep right edge fixed, grow leftward
            new_w = max(_NODE_MIN_W, orig.width() - delta_x)
            new_h = _NODE_TITLE_H + round(new_w * 9 / 16)
            new_x = orig.right() - new_w + 1
            if self.parent() is not None:
                new_x = max(0, new_x)
            self.setGeometry(new_x, orig.top(), new_w, new_h)

    def _on_resize_end(self, corner: str) -> None:
        self._resize_orig_geom = None

    # ------------------------------------------------------------------
    # Title-bar drag → move
    # ------------------------------------------------------------------

    def eventFilter(self, obj: object, event: QEvent) -> bool:
        if obj is self._title_bar:
            t = event.type()
            if t == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_global_start = event.globalPos()
                self._drag_node_start   = self.pos()
            elif t == QEvent.MouseMove and self._drag_global_start is not None:
                if event.buttons() & Qt.LeftButton:
                    delta   = event.globalPos() - self._drag_global_start
                    new_pos = self._drag_node_start + delta
                    if self.parent() is not None:
                        pw = self.parent().width()
                        ph = self.parent().height()
                        nx = max(0, min(new_pos.x(), pw - self.width()))
                        ny = max(0, min(new_pos.y(), ph - self.height()))
                        new_pos = QPoint(nx, ny)
                    self.move(new_pos)
                    if isinstance(self.parent(), SyncWorkspace):
                        self.parent().update()
            elif t == QEvent.MouseButtonRelease:
                self._drag_global_start = None
                self._drag_node_start   = None
        return super().eventFilter(obj, event)


# ---------------------------------------------------------------------------
# LiveVideoNode
# ---------------------------------------------------------------------------

class LiveVideoNode(SyncNode):
    """Workspace node with a resizable (16:9) live video preview."""

    def __init__(self, device: str = _VIDEO_DEVICE, parent: QWidget = None) -> None:
        # device=None means "unselected" — show placeholder, no capture
        self._device = device  # may be None
        super().__init__("", parent)

        # ── Title bar: single cam+device button + sound button ───────────
        tb = self._tb_layout
        tb.removeWidget(self._title_label)
        self._title_label.hide()

        label = self._device if self._device else "<select-input>"
        self._cam_btn = _TbBtn(icon_name="video-camera-solid", text=f"  {label}",
                               icon_size=14, parent=self._title_bar)
        self._cam_btn.setFont(theme.font_ui())
        self._cam_btn.setStyleSheet(_TB_TEXT_BTN_SS)
        self._cam_btn.clicked.connect(self._show_device_menu)
        tb.insertWidget(0, self._cam_btn, 0, Qt.AlignVCenter)

        self._snd_btn = _TbBtn(icon_name="sound-off", icon_size=14,
                               parent=self._title_bar)
        self._snd_btn.setFixedSize(22, 22)
        self._snd_btn.setStyleSheet(_TB_ICON_BTN_SS)
        self._snd_btn.clicked.connect(self._toggle_audio)
        tb.insertWidget(0, self._snd_btn, 0, Qt.AlignVCenter)

        self._audio_module_id: int | None = None

        # ── Video widget (only if a device was given) ─────────────────────
        self._video = LiveVideoWidget(self._device, self) if self._device else None
        if self._video:
            self.content_layout().addWidget(self._video)

    def node_type(self) -> str:
        return "live_video"

    def output_specs(self) -> dict:
        return {
            "image": {"type": "image", "shape": "half_circle", "dimension": None, "label": None},
        }

    def latest_frame_rgb(self):
        """Return the most recent captured frame, or None."""
        if self._video is not None:
            return self._video.latest_frame_rgb()
        return None

    def state_dict(self) -> dict:
        d = super().state_dict()
        d["device"] = self._device  # may be None
        d["audio"]  = self._audio_module_id is not None
        return d

    def _apply_title_chrome(self, show: bool) -> None:
        if show:
            self._title_bar.setStyleSheet(f"background: {_NODE_TITLE_BG};")
        else:
            self._title_bar.setStyleSheet("background: transparent;")
        self._close_btn.setVisible(show)
        self._cam_btn.setVisible(show)
        self._snd_btn.setVisible(show and bool(self._device))

    def _show_device_menu(self) -> None:
        devices = _enumerate_video_devices()
        used: set = set()
        ws = self.parent()
        if isinstance(ws, SyncWorkspace):
            used = ws.used_devices()
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {theme.PANEL_BG}; color: {theme.TEXT};"
            f"  border: 1px solid {theme.UI_BORDER}; padding: 2px; }}"
            f"QMenu::item {{ padding: 4px 16px; }}"
            f"QMenu::item:selected {{ background: {theme.ACCENT}; }}"
            f"QMenu::item:disabled {{ color: {theme.TEXT_DIM}; }}"
        )
        for dev in devices:
            action = menu.addAction(dev)
            if dev == self._device:
                action.setCheckable(True)
                action.setChecked(True)
            elif dev in used:
                action.setEnabled(False)
        btn = self._cam_btn
        pos = btn.mapToGlobal(QPoint(0, btn.height()))
        chosen = menu.exec_(pos)
        if chosen and chosen.isEnabled() and not chosen.isChecked():
            self._switch_device(chosen.text())

    def _toggle_audio(self) -> None:
        import subprocess as _sp
        if self._audio_module_id is not None:
            try:
                _sp.run(
                    ["pactl", "unload-module", str(self._audio_module_id)],
                    check=False, capture_output=True,
                )
            except Exception:
                pass
            self._audio_module_id = None
            self._snd_btn.set_icon("sound-off", theme.TEXT)
        else:
            source = _find_alsa_source_for_video(self._device)
            if not source:
                return
            try:
                result = _sp.run(
                    ["pactl", "load-module", "module-loopback",
                     f"source={source}", "latency_msec=1"],
                    capture_output=True, text=True, check=False,
                )
                self._audio_module_id = int(result.stdout.strip())
                self._snd_btn.set_icon("sound-high-solid", theme.TEXT)
            except Exception:
                pass

    def _switch_device(self, device: str) -> None:
        # Reset audio before switching
        if self._audio_module_id is not None:
            import subprocess as _sp
            try:
                _sp.run(
                    ["pactl", "unload-module", str(self._audio_module_id)],
                    check=False, capture_output=True,
                )
            except Exception:
                pass
            self._audio_module_id = None
            self._snd_btn.set_icon("sound-off", theme.TEXT)
        if self._video is not None:
            self._video.stop()
            self.content_layout().removeWidget(self._video)
            self._video.deleteLater()
        self._device = device
        self._cam_btn.setText(f"  {device}")
        self._snd_btn.setVisible(True)
        self._video = LiveVideoWidget(device, self)
        self.content_layout().addWidget(self._video)
        self._video.show()

    def _on_close(self) -> None:
        if self._audio_module_id is not None:
            import subprocess as _sp
            try:
                _sp.run(
                    ["pactl", "unload-module", str(self._audio_module_id)],
                    check=False, capture_output=True,
                )
            except Exception:
                pass
        if self._video is not None:
            self._video.stop()
        super()._on_close()


# ---------------------------------------------------------------------------
# SyncWorkspace — central drop canvas
# ---------------------------------------------------------------------------

class SyncWorkspace(QWidget):
    """Absolute-positioned canvas that accepts dropped palette items as nodes.

    Also manages cable drawing between node ports.
    """

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {_WORKSPACE_BG};")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self._nodes: list[SyncNode] = []
        self._chrome_visible: bool  = True

        # Connections: list of {source_node, source_port, target_node, target_port}
        self._connections: list[dict] = []

        # Cable drag state
        self._drag_src_node: SyncNode | None  = None
        self._drag_src_port: str | None       = None
        self._drag_src_is_output: bool        = True
        self._drag_cur_pos: QPoint | None     = None
        # Hover highlight during drag — compatible target port under cursor
        self._drag_hover_node: SyncNode | None = None
        self._drag_hover_port: str | None      = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def nodes(self) -> list[SyncNode]:
        return list(self._nodes)

    def nodes_state(self) -> list[dict]:
        return [n.state_dict() for n in self._nodes]

    def connections_state(self) -> list[dict]:
        return list(self._connections)

    def restore_nodes(self, states: list[dict]) -> None:
        for s in states:
            if not isinstance(s, dict):
                continue
            if s.get("type") == "live_video":
                device = s.get("device", _VIDEO_DEVICE)
                w = max(_NODE_MIN_W, int(s.get("w", _NODE_DEFAULT_W)))
                h = _NODE_TITLE_H + round(w * 9 / 16)
                node = self._make_live_video_node(device)
                node.node_id = int(s.get("node_id", node.node_id))
                node.setGeometry(int(s.get("x", 0)), int(s.get("y", 0)), w, h)
                node.show()
                if s.get("audio") and device:
                    node._toggle_audio()
            elif s.get("type") == "frame_vector":
                w = max(_NODE_MIN_W, int(s.get("w", _NODE_DEFAULT_W)))
                h = max(120, int(s.get("h", 160)))
                node = self._make_frame_vector_node()
                node.node_id = int(s.get("node_id", node.node_id))
                node.setGeometry(int(s.get("x", 0)), int(s.get("y", 0)), w, h)
                if "interval" in s:
                    node._set_interval(s["interval"])
                if s.get("star"):
                    node._toggle_star()
                if s.get("info"):
                    node._toggle_info()
                if not s.get("display", True):
                    node._toggle_display()
                node.show()
            elif s.get("type") == "frame_match":
                w = max(_FM_MIN_W, int(s.get("w", _FM_DEFAULT_W)))
                h = max(_NODE_TITLE_H + 80, int(s.get("h", _FM_DEFAULT_H)))
                node = self._make_frame_match_node()
                node.node_id = int(s.get("node_id", node.node_id))
                node.setGeometry(int(s.get("x", 0)), int(s.get("y", 0)), w, h)
                if "target_media" in s:
                    mt = s["target_media"]
                    if mt != node._media_type:
                        node._media_type = mt
                        node._media_btn.setText(f"  {mt}")
                if "scope_all" in s:
                    node._scope_all = bool(s["scope_all"])
                if "scope_title" in s:
                    node._scope_title = s["scope_title"]
                    if node._scope_title:
                        node._scope_btn.setText(f"  {node._scope_title}")
                if "top" in s:
                    node._top = int(s["top"])
                    node._top_btn.setText(f"  {{{node._top}}}")
                if s.get("info"):
                    node._toggle_info()
                node.show()
            elif s.get("type") == "frames_viewer":
                w = max(_FV2_MIN_W, int(s.get("w", _FV2_DEFAULT_W)))
                h = max(_NODE_TITLE_H + 80, int(s.get("h", _FV2_DEFAULT_H)))
                node = self._make_frames_viewer_node()
                node.node_id = int(s.get("node_id", node.node_id))
                node.setGeometry(int(s.get("x", 0)), int(s.get("y", 0)), w, h)
                if "zoom" in s:
                    node._zoom = float(s["zoom"])
                    node._zoom_btn.setText(f"  {int(node._zoom * 100)}%")
                if "mode" in s and s["mode"] in ("best", "loop"):
                    node._mode = s["mode"]
                    node._mode_btn.setText(f"  {node._mode}")
                node.show()
        # Advance the global ID counter past all restored IDs so new nodes
        # never collide with existing ones.
        if self._nodes:
            max_id = max(n.node_id for n in self._nodes)
            if max_id >= _next_node_id[0]:
                _next_node_id[0] = max_id + 1

    def restore_connections(self, states: list[dict]) -> None:
        id_map = {n.node_id: n for n in self._nodes}
        for c in states:
            src = id_map.get(c.get("source_node"))
            tgt = id_map.get(c.get("target_node"))
            if src and tgt:
                self._add_connection(src, c["source_port"], tgt, c["target_port"])

    def set_chrome_visible(self, visible: bool) -> None:
        self._chrome_visible = visible
        for node in self._nodes:
            node.set_chrome_visible(visible)
        self.update()

    def used_devices(self) -> set:
        """Return the set of device paths currently claimed by nodes (excludes unselected)."""
        return {n._device for n in self._nodes if isinstance(n, LiveVideoNode) and n._device}

    def is_port_connected(self, node_id: int, port: str, is_output: bool) -> bool:
        for c in self._connections:
            if is_output:
                if c["source_node"] == node_id and c["source_port"] == port:
                    return True
            else:
                if c["target_node"] == node_id and c["target_port"] == port:
                    return True
        return False

    def get_source_node_for_input(self, node_id: int, port: str) -> "SyncNode | None":
        """Return the source node connected to a given input port, or None."""
        id_map = {n.node_id: n for n in self._nodes}
        for c in self._connections:
            if c["target_node"] == node_id and c["target_port"] == port:
                return id_map.get(c["source_node"])
        return None

    def dispatch_output(self, source_node: "SyncNode", source_port: str,
                        value, meta: dict) -> None:
        """Fan-out a value from source_node.source_port to all connected inputs."""
        id_map = {n.node_id: n for n in self._nodes}
        for c in self._connections:
            if (c["source_node"] == source_node.node_id
                    and c["source_port"] == source_port):
                target = id_map.get(c["target_node"])
                if target is not None:
                    target.receive_input(c["target_port"], value, meta)

    # ------------------------------------------------------------------
    # Drag-and-drop (palette → workspace)
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(_MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(_MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasFormat(_MIME_TYPE):
            event.ignore()
            return
        item_type = bytes(event.mimeData().data(_MIME_TYPE)).decode()
        pos = event.pos()
        if item_type == "live_video":
            used = self.used_devices()
            all_devs = _enumerate_video_devices()
            free = next((d for d in all_devs if d not in used), None)
            node = self._make_live_video_node(free)
            x = max(0, min(pos.x() - _NODE_DEFAULT_W // 2,
                           self.width()  - _NODE_DEFAULT_W))
            y = max(0, min(pos.y() - _NODE_TITLE_H,
                           self.height() - _NODE_DEFAULT_H))
            node.move(x, y)
            node.show()
        elif item_type == "frame_vector":
            node = self._make_frame_vector_node()
            nw, nh = 300, 160
            x = max(0, min(pos.x() - nw // 2, self.width()  - nw))
            y = max(0, min(pos.y() - _NODE_TITLE_H, self.height() - nh))
            node.move(x, y)
            node.show()
        elif item_type == "frame_match":
            node = self._make_frame_match_node()
            nw, nh = _FM_DEFAULT_W, _FM_DEFAULT_H
            x = max(0, min(pos.x() - nw // 2, self.width()  - nw))
            y = max(0, min(pos.y() - _NODE_TITLE_H, self.height() - nh))
            node.move(x, y)
            node.show()
        elif item_type == "frames_viewer":
            node = self._make_frames_viewer_node()
            nw, nh = _FV2_DEFAULT_W, _FV2_DEFAULT_H
            x = max(0, min(pos.x() - nw // 2, self.width()  - nw))
            y = max(0, min(pos.y() - _NODE_TITLE_H, self.height() - nh))
            node.move(x, y)
            node.show()
        event.acceptProposedAction()

    # ------------------------------------------------------------------
    # Cable mouse events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        # Hit-test all node ports
        for node in self._nodes:
            if not node.isVisible():
                continue
            # Output port — tip is PORT_SIZE beyond the node right edge
            for port in node.output_ports():
                p = node.output_port_pos(port)
                if (event.pos() - p).manhattanLength() <= _PORT_SIZE + 8:
                    # Don't remove existing connection yet — wait for release
                    self._drag_src_node      = node
                    self._drag_src_port      = port
                    self._drag_src_is_output = True
                    self._drag_cur_pos       = event.pos()
                    self.update()
                    return
            # Input port — tip is PORT_SIZE beyond the node left edge
            for port in node.input_ports():
                p = node.input_port_pos(port)
                if (event.pos() - p).manhattanLength() <= _PORT_SIZE + 8:
                    # Don't remove existing connection yet — wait for release
                    self._drag_src_node      = node
                    self._drag_src_port      = port
                    self._drag_src_is_output = False
                    self._drag_cur_pos       = event.pos()
                    self.update()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_src_node is not None:
            self._drag_cur_pos = event.pos()
            # Find compatible target port under cursor for hover highlight
            hover_node, hover_port = None, None
            for node in self._nodes:
                if node is self._drag_src_node or not node.isVisible():
                    continue
                if self._drag_src_is_output:
                    for port in node.input_ports():
                        pp = node.input_port_pos(port)
                        if (event.pos() - pp).manhattanLength() <= _PORT_SIZE + 8:
                            if self._types_compatible(
                                    self._drag_src_node, self._drag_src_port, node, port):
                                hover_node, hover_port = node, port
                else:
                    for port in node.output_ports():
                        pp = node.output_port_pos(port)
                        if (event.pos() - pp).manhattanLength() <= _PORT_SIZE + 8:
                            if self._types_compatible(
                                    node, port, self._drag_src_node, self._drag_src_port):
                                hover_node, hover_port = node, port
            self._drag_hover_node = hover_node
            self._drag_hover_port = hover_port
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_src_node is None:
            super().mouseReleaseEvent(event)
            return
        if event.button() == Qt.LeftButton:
            # For input-port drags: remove the existing incoming cable first,
            # then attempt to connect to a new source.
            # For output-port drags: only remove if a new target is found
            # (fan-out — releasing into empty space keeps existing cables).
            if not self._drag_src_is_output:
                self._remove_connections_for(
                    self._drag_src_node.node_id, self._drag_src_port, is_output=False)
            completed = self._try_complete_connection(event.pos())
            if self._drag_src_is_output and completed:
                # Output drag that successfully connected: the _add_connection
                # call inside _try_complete_connection already handled the
                # input side; nothing extra needed here.
                pass
        self._drag_src_node   = None
        self._drag_src_port   = None
        self._drag_cur_pos    = None
        self._drag_hover_node = None
        self._drag_hover_port = None
        self.update()

    def _try_complete_connection(self, pos: QPoint) -> bool:
        """Try to snap the drag to a compatible port.  Returns True on success."""
        src = self._drag_src_node
        src_port = self._drag_src_port
        is_output = self._drag_src_is_output
        for node in self._nodes:
            if node is src or not node.isVisible():
                continue
            if is_output:
                for port in node.input_ports():
                    p = node.input_port_pos(port)
                    if (pos - p).manhattanLength() <= _PORT_SIZE + 8:
                        if self._types_compatible(src, src_port, node, port):
                            self._add_connection(src, src_port, node, port)
                            return True
                        return False
            else:
                for port in node.output_ports():
                    p = node.output_port_pos(port)
                    if (pos - p).manhattanLength() <= _PORT_SIZE + 8:
                        if self._types_compatible(node, port, src, src_port):
                            self._add_connection(node, port, src, src_port)
                            return True
                        return False
        return False

    def _types_compatible(self, src_node, src_port, tgt_node, tgt_port) -> bool:
        # No self-connections
        if src_node is tgt_node:
            return False
        src_specs = src_node.output_specs()
        tgt_specs = tgt_node.input_specs()
        if src_port not in src_specs or tgt_port not in tgt_specs:
            return False
        src_spec = src_specs[src_port]
        tgt_spec = tgt_specs[tgt_port]
        # Types must match
        if src_spec["type"] != tgt_spec["type"]:
            return False
        # Dimensions must match if both specified
        src_dim = src_spec.get("dimension")
        tgt_dim = tgt_spec.get("dimension")
        if src_dim is not None and tgt_dim is not None and src_dim != tgt_dim:
            return False
        return True

    # ------------------------------------------------------------------
    # Connection helpers (fan-out: one output → many inputs;
    #                     one-to-one: each input has at most one source)
    # ------------------------------------------------------------------

    def _connection_exists(self, src_id: int, src_port: str,
                           tgt_id: int, tgt_port: str) -> bool:
        return any(
            c["source_node"] == src_id and c["source_port"] == src_port
            and c["target_node"] == tgt_id and c["target_port"] == tgt_port
            for c in self._connections
        )

    def _remove_connection_to_input(self, tgt_id: int, tgt_port: str) -> None:
        """Remove only the incoming cable on one input port."""
        id_map = {n.node_id: n for n in self._nodes}
        kept = []
        for c in self._connections:
            if c["target_node"] == tgt_id and c["target_port"] == tgt_port:
                tgt = id_map.get(c["target_node"])
                self._notify_disconnected(tgt)
            else:
                kept.append(c)
        self._connections = kept

    def _notify_connected(self, tgt: "SyncNode", src: "SyncNode") -> None:
        if hasattr(tgt, "on_connected"):
            tgt.on_connected(src)

    def _notify_disconnected(self, tgt: "SyncNode | None") -> None:
        if tgt is not None and hasattr(tgt, "on_disconnected"):
            tgt.on_disconnected()

    def _add_connection(self, src: "SyncNode", src_port: str,
                        tgt: "SyncNode", tgt_port: str) -> None:
        # Enforce one-to-one on the input side: replace existing incoming cable
        self._remove_connection_to_input(tgt.node_id, tgt_port)
        # Guard against duplicates (shouldn't happen, but be safe)
        if not self._connection_exists(src.node_id, src_port,
                                       tgt.node_id, tgt_port):
            self._connections.append({
                "source_node": src.node_id, "source_port": src_port,
                "target_node": tgt.node_id, "target_port": tgt_port,
            })
        self._notify_connected(tgt, src)
        src.update()
        tgt.update()
        self.update()

    def _remove_connections_for(self, node_id: int, port: str,
                                is_output: bool) -> None:
        """Remove all connections touching a given port.

        For input ports (is_output=False) this removes the single incoming
        cable.  For output ports (is_output=True) this removes ALL outgoing
        cables — used only when dragging from an output (mouseRelease into
        empty space is a no-op for outputs per fan-out policy).
        """
        id_map = {n.node_id: n for n in self._nodes}
        kept = []
        for c in self._connections:
            if is_output and c["source_node"] == node_id and c["source_port"] == port:
                self._notify_disconnected(id_map.get(c["target_node"]))
            elif not is_output and c["target_node"] == node_id and c["target_port"] == port:
                self._notify_disconnected(id_map.get(c["target_node"]))
            else:
                kept.append(c)
        self._connections = kept
        self.update()

    # ------------------------------------------------------------------
    # Cable painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._chrome_visible:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen_w = _CABLE_WIDTH

        # Draw completed cables
        id_map = {n.node_id: n for n in self._nodes}
        for c in self._connections:
            src_node = id_map.get(c["source_node"])
            tgt_node = id_map.get(c["target_node"])
            if src_node and tgt_node:
                p1 = src_node.output_port_pos(c["source_port"])
                p2 = tgt_node.input_port_pos(c["target_port"])
                p.setPen(QPen(_CABLE_COLOR_CONN, pen_w))
                self._draw_cable(p, p1, p2)

        # Draw active drag cable
        if self._drag_src_node and self._drag_cur_pos:
            if self._drag_src_is_output:
                p1 = self._drag_src_node.output_port_pos(self._drag_src_port)
                p2 = self._drag_cur_pos
            else:
                p1 = self._drag_cur_pos
                p2 = self._drag_src_node.input_port_pos(self._drag_src_port)
            p.setPen(QPen(_CABLE_COLOR_DRAG, pen_w))
            self._draw_cable(p, p1, p2)

        # Draw port shapes for all nodes — always visible, positioned OUTSIDE
        # node bounds so clicks on them reach this workspace mousePressEvent.
        p.setPen(Qt.NoPen)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = _PORT_SIZE
        for node in self._nodes:
            if not node.isVisible():
                continue
            cy = node.y() + node.height() // 2
            out_specs = node.output_specs()
            for port in node.output_ports():
                bx = node.x() + node.width()   # base/center at right edge
                tx = bx + s                     # tip extends right
                if (self._drag_src_node is node
                        and self._drag_src_port == port
                        and self._drag_src_is_output):
                    color = _PORT_COLOR_ACTIVE
                elif (self._drag_hover_node is node and self._drag_hover_port == port):
                    color = _PORT_COLOR_ACTIVE
                elif self.is_port_connected(node.node_id, port, True):
                    color = _PORT_COLOR_CONN
                else:
                    color = _PORT_COLOR_IDLE
                p.setBrush(color)
                spec = out_specs.get(port, {})
                if spec.get("shape") == "half_circle":
                    # Right-facing semicircle: flat edge at node right, dome extends right
                    path = QPainterPath()
                    path.moveTo(bx, cy - s)
                    path.arcTo(QRectF(bx - s, cy - s, 2 * s, 2 * s), 90, -180)
                    path.closeSubpath()
                    p.drawPath(path)
                elif spec.get("shape") == "object":
                    # Filled } shape: rectangle tab with V-notch on left (opening) side
                    half  = s
                    notch = int(s * 0.45)
                    obj_path = QPainterPath()
                    obj_path.moveTo(bx,         cy - half)
                    obj_path.lineTo(bx + half,  cy - half)
                    obj_path.lineTo(bx + half,  cy + half)
                    obj_path.lineTo(bx,         cy + half)
                    obj_path.lineTo(bx,         cy + notch)
                    obj_path.lineTo(bx + notch, cy)           # notch tip pointing right
                    obj_path.lineTo(bx,         cy - notch)
                    obj_path.closeSubpath()
                    p.drawPath(obj_path)
                else:
                    # Default: right-pointing triangle
                    p.drawPolygon(QPolygon([
                        QPoint(bx, cy - s // 2),
                        QPoint(tx, cy),
                        QPoint(bx, cy + s // 2),
                    ]))
                label = spec.get("label")
                if label:
                    p.setPen(color)
                    p.setFont(self.font())
                    fm = p.fontMetrics()
                    lw = fm.horizontalAdvance(label)
                    # Draw just outside node.right(), above the port shape
                    p.drawText(bx + 2, cy - s // 2 - 2, label)
                    p.setPen(Qt.NoPen)

            in_specs = node.input_specs()
            for port in node.input_ports():
                bx = node.x()    # base/center at left edge
                tx = bx - s      # tip extends left
                if (self._drag_src_node is node
                        and self._drag_src_port == port
                        and not self._drag_src_is_output):
                    color = _PORT_COLOR_ACTIVE
                elif (self._drag_hover_node is node and self._drag_hover_port == port):
                    color = _PORT_COLOR_ACTIVE
                elif self.is_port_connected(node.node_id, port, False):
                    color = _PORT_COLOR_CONN
                else:
                    color = _PORT_COLOR_IDLE
                p.setBrush(color)
                spec = in_specs.get(port, {})
                if spec.get("shape") == "half_circle":
                    # Left-facing semicircle: flat edge at node left, dome extends left
                    path = QPainterPath()
                    path.moveTo(bx, cy - s)
                    path.arcTo(QRectF(bx - s, cy - s, 2 * s, 2 * s), 90, 180)
                    path.closeSubpath()
                    p.drawPath(path)
                elif spec.get("shape") == "object":
                    # Filled { shape: rectangle tab with V-notch on right (opening) side
                    half  = s
                    notch = int(s * 0.45)
                    obj_path = QPainterPath()
                    obj_path.moveTo(bx,          cy - half)
                    obj_path.lineTo(bx,          cy - notch)
                    obj_path.lineTo(bx - notch,  cy)           # notch tip pointing left
                    obj_path.lineTo(bx,          cy + notch)
                    obj_path.lineTo(bx,          cy + half)
                    obj_path.lineTo(bx - half,   cy + half)
                    obj_path.lineTo(bx - half,   cy - half)
                    obj_path.closeSubpath()
                    p.drawPath(obj_path)
                else:
                    # Default: left-pointing triangle
                    p.drawPolygon(QPolygon([
                        QPoint(bx, cy - s // 2),
                        QPoint(tx, cy),
                        QPoint(bx, cy + s // 2),
                    ]))
                label = spec.get("label")
                if label:
                    p.setPen(color)
                    p.setFont(self.font())
                    fm = p.fontMetrics()
                    lw = fm.horizontalAdvance(label)
                    # Draw just outside node.left(), above the port shape
                    p.drawText(bx - lw - 2, cy - s // 2 - 2, label)
                    p.setPen(Qt.NoPen)

        p.end()

    def _draw_cable(self, painter: QPainter, p1: QPoint, p2: QPoint) -> None:
        """Draw a bezier cable from p1 (output) to p2 (input).

        Control points always exit/enter horizontally so the cable visibly
        leaves the output port to the right and arrives at the input port
        from the left.

        Forward connections (input to the right of output, dx >= 0):
          tension is proportional to the vertical offset only, so cables that
          are nearly level (small dy) flatten to a straight line.

        Backward connections (input to the left of output, dx < 0):
          tension uses horizontal span with a floor so the cable arcs
          visibly outward before doubling back.
        """
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        if dx >= 0:
            tension = int(abs(dy) * 0.5)
        else:
            tension = int(max(abs(dx) * 0.5, 80))
        path = QPainterPath()
        path.moveTo(p1)
        path.cubicTo(
            QPoint(p1.x() + tension, p1.y()),   # exits rightward from output
            QPoint(p2.x() - tension, p2.y()),   # enters leftward into input
            p2,
        )
        painter.drawPath(path)

    # ------------------------------------------------------------------
    # Node factories
    # ------------------------------------------------------------------

    def _make_live_video_node(self, device: str) -> "LiveVideoNode":
        node = LiveVideoNode(device=device, parent=self)
        node.closed.connect(self._on_node_closed)
        node.set_chrome_visible(self._chrome_visible)
        self._nodes.append(node)
        return node

    def _make_frame_vector_node(self) -> "FrameVectorNode":
        node = FrameVectorNode(parent=self)
        node.closed.connect(self._on_node_closed)
        node.set_chrome_visible(self._chrome_visible)
        self._nodes.append(node)
        return node

    def _make_frame_match_node(self) -> "FrameMatchNode":
        node = FrameMatchNode(parent=self)
        node.closed.connect(self._on_node_closed)
        node.set_chrome_visible(self._chrome_visible)
        self._nodes.append(node)
        return node

    def _make_frames_viewer_node(self) -> "FramesViewerNode":
        node = FramesViewerNode(parent=self)
        node.closed.connect(self._on_node_closed)
        node.set_chrome_visible(self._chrome_visible)
        self._nodes.append(node)
        return node

    def _on_node_closed(self, node: SyncNode) -> None:
        # Remove all connections involving this node
        self._connections = [
            c for c in self._connections
            if c["source_node"] != node.node_id and c["target_node"] != node.node_id
        ]
        if node in self._nodes:
            self._nodes.remove(node)
        self.update()


# ---------------------------------------------------------------------------
# _EmbedWorker — background thread for CLIP frame embedding
# ---------------------------------------------------------------------------

class _EmbedWorker(QObject):
    """Runs CLIP embedding on a single frame in a worker thread.

    Signals:
        result(np.ndarray, str)  — vector + model name on success
        error(str)               — error message on failure
    """
    result = pyqtSignal(object, str)
    error  = pyqtSignal(str)

    def __init__(self, frame_rgb, project_path: str = ".") -> None:
        super().__init__()
        self._frame_rgb    = frame_rgb
        self._project_path = project_path

    def run(self) -> None:
        try:
            from services.frame_vector import load_frame_vector_model, embed_rgb_frame
            bundle = load_frame_vector_model(self._project_path)
            vec = embed_rgb_frame(self._frame_rgb, bundle)
            model_name = bundle[3]
            try:
                self.result.emit(vec, model_name)
            except RuntimeError:
                pass
        except Exception as exc:
            try:
                self.error.emit(str(exc))
            except RuntimeError:
                pass


# ---------------------------------------------------------------------------
class _VectorStarOverlay(QWidget):
    """Transparent overlay that draws the vector as a radial polygon."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._vec = None  # np.ndarray or None

    def set_vector(self, vec) -> None:
        self._vec = vec
        self.update()

    def paintEvent(self, event) -> None:
        if self._vec is None or len(self._vec) < 3:
            return
        import math
        import numpy as np
        vec = np.asarray(self._vec, dtype=float)

        max_abs = np.percentile(np.abs(vec), 99)
        if max_abs < 1e-9:
            return

        w = self.width()
        h = self.height()
        content_top = _NODE_TITLE_H
        content_h = h - content_top
        cx = w / 2.0
        cy = content_top + content_h / 2.0
        max_r  = min(w, content_h) / 2.0 * 0.75
        half_r = max_r * 0.5
        n = len(vec)

        # Pre-compute (point, radius) pairs
        pts = []
        radii = []
        for i in range(n):
            angle = 2 * math.pi * i / n - math.pi / 2
            r = half_r + (vec[i] / max_abs) * half_r
            r = max(0.0, r)
            pts.append(QPointF(cx + r * math.cos(angle), cy + r * math.sin(angle)))
            radii.append(r)

        # Normalise saturation against the actual radius range so the
        # innermost point is always pure white and the outermost pure red.
        r_min = min(radii)
        r_max = max(radii)
        r_span = r_max - r_min if r_max > r_min else 1.0

        # Pre-extract colour A/B components for fast lerp
        ar, ag, ab = _STAR_COLOR_A.red(), _STAR_COLOR_A.green(), _STAR_COLOR_A.blue()
        br, bg, bb = _STAR_COLOR_B.red(), _STAR_COLOR_B.green(), _STAR_COLOR_B.blue()

        def _color(r: float, alpha: float = 1.0) -> QColor:
            """Linear interpolation from _STAR_COLOR_A (inner) to _STAR_COLOR_B (outer)."""
            t = (r - r_min) / r_span
            return QColor(
                int(ar + t * (br - ar)),
                int(ag + t * (bg - ag)),
                int(ab + t * (bb - ab)),
                int(alpha * 255),
            )

        center = QPointF(cx, cy)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # ── Dominant colour fill (32-bin histogram on t) ─────────────────
        _BINS = 32
        bins = [0] * _BINS
        for i in range(n):
            j = (i + 1) % n
            mean_r = (radii[i] + radii[j]) / 2.0
            t = (mean_r - r_min) / r_span
            b = min(_BINS - 1, int(t * _BINS))
            bins[b] += 1
        dominant_t = (bins.index(max(bins)) + 0.5) / _BINS
        dominant_color = QColor(
            int(ar + dominant_t * (br - ar)),
            int(ag + dominant_t * (bg - ag)),
            int(ab + dominant_t * (bb - ab)),
            int(0.35 * 255),
        )
        poly_path = QPainterPath()
        poly_path.moveTo(pts[0])
        for i in range(1, n):
            poly_path.lineTo(pts[i])
        poly_path.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(dominant_color)
        painter.drawPath(poly_path)

        # ── Outline segments on top ───────────────────────────────────────
        painter.setBrush(Qt.NoBrush)
        for i in range(n):
            j = (i + 1) % n
            mean_r = (radii[i] + radii[j]) / 2.0
            pen = QPen(_color(mean_r))
            pen.setWidthF(_PEN_WIDTH)
            painter.setPen(pen)
            painter.drawLine(pts[i], pts[j])

        painter.end()


# FrameVectorNode — embeds live video frames with CLIP
# ---------------------------------------------------------------------------

_FV_INTERVALS = [None, 0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 5.0]   # None = off
_FV_DEFAULT_INTERVAL = 2.0

class FrameVectorNode(SyncNode):
    """Node that receives image frames and displays their embedding vector."""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__("", parent)

        self._interval_s: float | None = _FV_DEFAULT_INTERVAL
        self._display_on: bool = True
        self._star_on:    bool = False
        self._info_on:    bool = False
        self._status_msg: str  = "waiting for connection\u2026"
        self._source_node: "LiveVideoNode | None" = None
        self._embed_busy: bool = False
        self._model_name: str  = ""
        self._vec_dim: int     = 0
        self._last_vec         = None   # np.ndarray or None

        # ── Title bar: scanning toggle + alarm icon + interval button ────
        self._title_label.hide()

        self._scan_btn = _TbBtn(icon_name="calculator-solid", icon_size=14,
                                 parent=self._title_bar)
        self._scan_btn.setFixedSize(22, 22)
        self._scan_btn.setStyleSheet(_TB_ICON_BTN_SS)
        self._scan_btn.clicked.connect(self._toggle_display)
        self._tb_layout.insertWidget(0, self._scan_btn, 0, Qt.AlignVCenter)
        self._update_scan_icon()

        self._star_btn = _TbBtn(icon_name="star-dashed", icon_size=14,
                                parent=self._title_bar)
        self._star_btn.setFixedSize(22, 22)
        self._star_btn.setStyleSheet(_TB_ICON_BTN_SS)
        self._star_btn.clicked.connect(self._toggle_star)
        self._tb_layout.insertWidget(1, self._star_btn, 0, Qt.AlignVCenter)
        self._update_star_icon()

        self._info_btn = _TbBtn(icon_name="info-circle", icon_size=14,
                                parent=self._title_bar)
        self._info_btn.setFixedSize(22, 22)
        self._info_btn.setStyleSheet(_TB_ICON_BTN_SS)
        self._info_btn.clicked.connect(self._toggle_info)
        self._tb_layout.insertWidget(2, self._info_btn, 0, Qt.AlignVCenter)
        self._update_info_icon()

        self._interval_btn = _TbBtn(icon_name="alarm-solid", text="off",
                                    icon_size=14, parent=self._title_bar)
        self._interval_btn.setText(f"{self._interval_s:.1f}s" if self._interval_s is not None else "off")
        self._interval_btn.setFont(theme.font_ui())
        self._interval_btn.setStyleSheet(_TB_TEXT_BTN_SS)
        self._interval_btn.clicked.connect(self._show_interval_menu)
        self._tb_layout.insertWidget(3, self._interval_btn, 0, Qt.AlignVCenter)

        # ── Content ──────────────────────────────────────────────────────
        self._body = QWidget(self)
        body = self._body
        body.setStyleSheet("background: transparent;")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(8, 6, 8, 6)
        blay.setSpacing(4)

        lbl_style = "color: #999999; background: transparent; font-size: 11px;"

        self._vec_text = QTextEdit(body)
        self._vec_text.setReadOnly(True)
        self._vec_text.setFont(theme.font_mono() if hasattr(theme, "font_mono") else theme.font_ui())
        self._vec_text.setStyleSheet(
            f"background: transparent; color: {_NODE_TEXT_COLOR};"
            "border: none; font-size: 10px;"
        )
        self._vec_text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._vec_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._vec_text.setPlainText("status: waiting for connection…")
        blay.addWidget(self._vec_text, 1)

        self.content_layout().addWidget(body)
        self.resize(300, 160)

        # Transparent overlay for vector star drawing (always on top)
        self._star_overlay = _VectorStarOverlay(self)
        self._star_overlay.setVisible(False)

        # Edge + corner resize handles — free per-edge/corner resize.
        self._handle_bl.hide()
        self._handle_br.hide()
        self._edge_resize_geom = None
        self._edge_handles: dict = {}
        _edge_cursors = {
            "top":    Qt.SizeVerCursor,
            "bottom": Qt.SizeVerCursor,
            "left":   Qt.SizeHorCursor,
            "right":  Qt.SizeHorCursor,
            "tl":     Qt.SizeFDiagCursor,
            "tr":     Qt.SizeBDiagCursor,
            "bl":     Qt.SizeBDiagCursor,
            "br":     Qt.SizeFDiagCursor,
        }
        for _edge in _edge_cursors:
            _eh = _EdgeResizeHandle(_edge, self)
            _eh.setCursor(_edge_cursors[_edge])
            _eh.resize_started.connect(self._on_edge_resize_start)
            _eh.resize_dragged.connect(self._on_edge_resize_drag)
            _eh.resize_released.connect(self._on_edge_resize_end)
            self._edge_handles[_edge] = _eh
        self._reposition_edge_handles()

        # Timer that fires the embedding loop
        self._timer = QTimer(self)
        if self._interval_s is not None:
            self._timer.setInterval(int(self._interval_s * 1000))
        self._timer.timeout.connect(self._on_tick)
        self._thread: QThread | None = None
        self._worker: _EmbedWorker | None = None

        # Ensure cleanup if the whole application quits while we're running
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._cleanup_thread)

    # ------------------------------------------------------------------
    # Port API
    # ------------------------------------------------------------------

    def node_type(self) -> str:
        return "frame_vector"

    def input_specs(self) -> dict:
        return {
            "image": {"type": "image", "shape": "half_circle", "dimension": None, "label": None},
        }

    def output_specs(self) -> dict:
        return {
            "vector": {"type": "vector", "shape": "triangle", "dimension": 512, "label": "[512]"},
        }

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._star_overlay.setGeometry(0, 0, self.width(), self.height())
        self._reposition_edge_handles()
        self._update_vec_display()

    def _reposition_handles(self) -> None:
        """Override base: keep the 16:9-enforcing corner handles permanently hidden."""
        self._handle_bl.hide()
        self._handle_br.hide()

    def _count_displayable_values(self) -> int:
        """Estimate how many vector values fit in the current text area."""
        # Each formatted value is '+0.123' (6 chars) + ', ' (2) = 8 chars wide.
        fm = self._vec_text.fontMetrics()
        char_w = fm.horizontalAdvance("0")  # monospace — every char same width
        line_h = fm.lineSpacing()
        margins = self._body.layout().contentsMargins()
        area_w = self.width()  - margins.left() - margins.right()  - 16
        area_h = self.height() - _NODE_TITLE_H - margins.top() - margins.bottom() - 16
        # Header: status + model + dims (3 lines) when info is on, 0 otherwise
        header_lines = 3 if self._info_on else 0
        avail_lines  = max(1, (area_h // max(1, line_h)) - header_lines)
        # Each value: '+0.123' = 6 chars + ', ' = 8 chars total
        value_chars  = 8
        values_per_line = max(1, area_w // max(1, char_w * value_chars))
        return avail_lines * values_per_line

    def _set_status(self, msg: str) -> None:
        """Update the status line and refresh the display."""
        self._status_msg = msg
        self._update_vec_display()

    def _update_vec_display(self) -> None:
        lines = []
        # Status line visible whenever the body is visible (info or vectors on)
        if self._info_on:
            lines.append(f"status: {getattr(self, '_status_msg', 'waiting…')}")
            lines.append(f"model: {self._model_name}" if self._model_name else "model: —")
            lines.append(f"dims:  {self._vec_dim}" if self._vec_dim else "dims:  —")
        # Vector values — only when display is ON and we have data
        if self._display_on and self._last_vec is not None:
            vec = self._last_vec
            n = self._count_displayable_values()
            preview = ", ".join(f"{v:+6.3f}" for v in vec[:n])
            if len(vec) > n:
                preview += ", …"
            lines.append(f"vector:[{preview}]")
        elif not self._info_on:
            # Neither on — show bare status so the widget isn't blank if body is somehow visible
            lines.append(f"status: {getattr(self, '_status_msg', 'waiting…')}")
        self._vec_text.setPlainText("\n".join(lines))

    def _reposition_edge_handles(self) -> None:
        t = _EDGE_THICKNESS
        w, h = self.width(), self.height()
        eh = self._edge_handles
        # Edges (corners cut out)
        eh["top"].setGeometry(t, 0, w - 2 * t, t)
        eh["bottom"].setGeometry(t, h - t, w - 2 * t, t)
        eh["left"].setGeometry(0, t, t, h - 2 * t)
        eh["right"].setGeometry(w - t, t, t, h - 2 * t)
        # Corners
        eh["tl"].setGeometry(0,     0,     t, t)
        eh["tr"].setGeometry(w - t, 0,     t, t)
        eh["bl"].setGeometry(0,     h - t, t, t)
        eh["br"].setGeometry(w - t, h - t, t, t)
        for handle in eh.values():
            handle.raise_()
        self._star_overlay.raise_()

    def _on_edge_resize_start(self, edge: str) -> None:
        self._edge_resize_geom = self.geometry()

    def _on_edge_resize_drag(self, edge: str, dx: int, dy: int) -> None:
        orig = self._edge_resize_geom
        if orig is None:
            return
        # Compute new w/h/x/y per affected axis
        new_x, new_y = orig.left(), orig.top()
        new_w, new_h = orig.width(), orig.height()
        if edge in ("right", "tr", "br"):
            new_w = max(_NODE_MIN_W, orig.width() + dx)
        if edge in ("left", "tl", "bl"):
            new_w = max(_NODE_MIN_W, orig.width() - dx)
            new_x = max(0, orig.right() - new_w + 1)
        if edge in ("bottom", "bl", "br"):
            new_h = max(_FV_MIN_H, orig.height() + dy)
        if edge in ("top", "tl", "tr"):
            new_h = max(_FV_MIN_H, orig.height() - dy)
            new_y = max(0, orig.bottom() - new_h + 1)
        self.setGeometry(new_x, new_y, new_w, new_h)

    def _on_edge_resize_end(self, edge: str) -> None:
        self._edge_resize_geom = None


    # ------------------------------------------------------------------

    def state_dict(self) -> dict:
        d = super().state_dict()
        d["interval"] = self._interval_s
        d["star"]     = self._star_on
        d["display"]  = self._display_on
        d["info"]     = self._info_on
        return d

    def _apply_title_chrome(self, show: bool) -> None:
        super()._apply_title_chrome(show)
        # Also hide/show the extra widgets unique to this node
        if hasattr(self, "_scan_btn"):
            self._scan_btn.setVisible(show)
        if hasattr(self, "_star_btn"):
            self._star_btn.setVisible(show)
        if hasattr(self, "_info_btn"):
            self._info_btn.setVisible(show)
        if hasattr(self, "_interval_btn"):
            self._interval_btn.setVisible(show)

    def _toggle_display(self) -> None:
        self._display_on = not self._display_on
        self._update_scan_icon()
        self._refresh_body_visibility()
        self._update_vec_display()

    def _toggle_info(self) -> None:
        self._info_on = not self._info_on
        self._update_info_icon()
        self._refresh_body_visibility()
        self._update_vec_display()

    def _refresh_body_visibility(self) -> None:
        """Body (text area) is visible when either info or vectors are on."""
        self._body.setVisible(self._display_on or self._info_on)

    def _toggle_star(self) -> None:
        self._star_on = not self._star_on
        self._update_star_icon()
        self._star_overlay.setVisible(self._star_on)
        if self._star_on and self._last_vec is not None:
            self._star_overlay.set_vector(self._last_vec)

    def _update_scan_icon(self) -> None:
        if self._display_on:
            self._scan_btn.set_icon("calculator-solid", theme.TEXT)
        else:
            self._scan_btn.set_icon("calculator", theme.TEXT_DIM)

    def _update_star_icon(self) -> None:
        if self._star_on:
            self._star_btn.set_icon("star-solid", theme.TEXT)
        else:
            self._star_btn.set_icon("star-dashed", theme.TEXT_DIM)

    def _update_info_icon(self) -> None:
        if self._info_on:
            self._info_btn.set_icon("info-circle-solid", theme.TEXT)
        else:
            self._info_btn.set_icon("info-circle", theme.TEXT_DIM)

    def _show_interval_menu(self) -> None:
        menu = QMenu(self)
        for v in _FV_INTERVALS:
            label = "off" if v is None else f"{v:.2f}s".rstrip('0').rstrip('.')
            label = label if not label.endswith('.') else label + '0'
            action = menu.addAction(label)
            action.setData(v)
            if v == self._interval_s:
                action.setCheckable(True)
                action.setChecked(True)
        chosen = menu.exec_(self._interval_btn.mapToGlobal(
            QPoint(0, self._interval_btn.height())))
        if chosen is not None:
            self._set_interval(chosen.data())

    def _set_interval(self, seconds) -> None:
        self._interval_s = seconds
        self._timer.stop()
        if seconds is None:
            self._interval_btn.setText("off")
            # Fire a single immediate tick if already connected
            if self._source_node is not None:
                QTimer.singleShot(0, self._on_tick)
        else:
            label = f"{seconds:.2f}s".rstrip('0').rstrip('.')
            if label.endswith('.'):
                label += '0'
            self._interval_btn.setText(label)
            self._timer.setInterval(int(seconds * 1000))
            # Start the timer whenever a source is connected
            if self._source_node is not None:
                self._timer.start()
        ws = self.parent()
        if isinstance(ws, SyncWorkspace):
            main = ws.parent()
            if hasattr(main, "_save_state"):
                main._save_state()

    def on_connected(self, source_node: "LiveVideoNode") -> None:
        self._source_node = source_node
        self._set_status("connected, waiting for frame…")
        if self._interval_s is not None:
            self._timer.setInterval(int(self._interval_s * 1000))
            self._timer.start()
        else:
            # "off" mode: fire once immediately on connect
            QTimer.singleShot(0, self._on_tick)

    def on_disconnected(self) -> None:
        self._source_node = None
        self._timer.stop()
        self._embed_busy = False
        self._set_status("waiting for connection…")
        self._vec_text.setPlainText("status: waiting for connection…")

    # ------------------------------------------------------------------
    # Embedding loop
    # ------------------------------------------------------------------

    def _on_tick(self) -> None:
        if self._embed_busy or self._source_node is None:
            return
        frame = self._source_node.latest_frame_rgb()
        if frame is None:
            self._set_status("waiting for image…")
            return
        self._embed_busy = True
        self._set_status("embedding…")

        # Find project path (best-effort)
        try:
            from tool import prefs as _p
            project_path = _p.get("path") or "."
        except Exception:
            project_path = "."

        # Worker thread — no parent so Qt won't destroy it under us
        import numpy as np
        self._worker_frame = np.array(frame)
        thread = QThread()   # no parent — we manage lifetime explicitly
        worker = _EmbedWorker(self._worker_frame, project_path)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result.connect(self._on_embed_result)
        worker.error.connect(self._on_embed_error)
        worker.result.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_embed_result(self, vec, model_name: str) -> None:
        import numpy as np
        import time as _time
        self._embed_busy = False
        self._model_name = model_name
        self._last_vec   = vec
        self._vec_dim    = len(vec)
        self._set_status("ok")
        if self._star_on:
            self._star_overlay.set_vector(vec)
        # Push vector to all connected downstream nodes
        self.emit_output("vector", vec, {
            "type": "vector",
            "dimension": int(len(vec)),
            "model": model_name,
            "source_node": self.node_id,
            "timestamp": _time.time(),
        })

    def _on_embed_error(self, msg: str) -> None:
        self._embed_busy = False
        self._set_status(f"error: {msg}")

    # ------------------------------------------------------------------
    # Clean up timer/thread on close
    # ------------------------------------------------------------------

    def _cleanup_thread(self) -> None:
        """Stop timer and wait for any running worker thread to finish."""
        self._timer.stop()
        thread = self._thread
        self._thread = None
        self._worker = None
        self._embed_busy = False
        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(500)
            except RuntimeError:
                pass

    def _on_close(self) -> None:
        self._cleanup_thread()
        super()._on_close()


# ---------------------------------------------------------------------------
# _FrameMatchWorker — background CLIP embed + catalog search
# ---------------------------------------------------------------------------

class _FrameMatchWorker(QObject):
    """Embed a frame and search the catalog in a worker thread.

    Uses the module-level catalog cache in services.sync_frame_match so the
    catalog is only loaded once per (project, media_type, scope).
    """
    results = pyqtSignal(object)   # list[dict]
    error   = pyqtSignal(str)

    def __init__(
        self,
        frame_rgb,
        project_path: str,
        media_type:   str,
        title:        "str | None",
        all_items:    bool,
        top:          int,
    ) -> None:
        super().__init__()
        self._frame_rgb   = frame_rgb
        self._project_path = project_path
        self._media_type   = media_type
        self._title        = title
        self._all_items    = all_items
        self._top          = top

    def run(self) -> None:
        try:
            from services.sync_frame_match import match_rgb_frame
            hits = match_rgb_frame(
                self._frame_rgb,
                self._project_path,
                self._media_type,
                title=self._title,
                all_items=self._all_items,
                top=self._top,
            )
            try:
                self.results.emit(hits)
            except RuntimeError:
                pass
        except Exception as exc:
            try:
                self.error.emit(str(exc))
            except RuntimeError:
                pass


# ---------------------------------------------------------------------------
# _CatalogSearchWorker — catalog-only search (no embedding)
# ---------------------------------------------------------------------------

class _CatalogSearchWorker(QObject):
    """Search the frame catalog for a pre-computed vector in a worker thread.

    The catalog cache in services.sync_frame_match means the catalog is only
    loaded from disk once per (project, media_type, scope) key.
    """
    results = pyqtSignal(object)   # list[dict]
    error   = pyqtSignal(str)

    def __init__(
        self,
        vector,
        project_path: str,
        media_type:   str,
        title:        "str | None",
        all_items:    bool,
        top:          int,
    ) -> None:
        super().__init__()
        self._vector       = vector
        self._project_path = project_path
        self._media_type   = media_type
        self._title        = title
        self._all_items    = all_items
        self._top          = top

    def run(self) -> None:
        try:
            from services.sync_frame_match import load_frame_catalog, match_frame_vector
            catalog = load_frame_catalog(
                self._project_path, self._media_type,
                title=self._title, all_items=self._all_items,
            )
            hits = match_frame_vector(self._vector, catalog, top=self._top)
            try:
                self.results.emit(hits)
            except RuntimeError:
                pass
        except Exception as exc:
            try:
                self.error.emit(str(exc))
            except RuntimeError:
                pass


# ---------------------------------------------------------------------------
# FrameMatchNode — finds closest indexed shot to the incoming live frame
# ---------------------------------------------------------------------------

_FM_DEFAULT_INTERVAL = 2.0
_FM_DEFAULT_TOP      = 5
_FM_MIN_W            = 260
_FM_DEFAULT_W        = 380
_FM_DEFAULT_H        = 240

# FramesViewerNode defaults
_FV2_DEFAULT_W       = 520
_FV2_DEFAULT_H       = 300
_FV2_MIN_W           = 260
_FM_TOP_OPTIONS      = (5, 3, 1)
_FRAME_NATURAL_W     = 1920   # reference pixel width for zoom % calculations


class FrameMatchNode(SyncNode):
    """Workspace node that matches live video frames against indexed catalogs."""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__("", parent)
        self._source_node:   "LiveVideoNode | None" = None
        self._media_type:    str   = "movie"
        self._scope_all:     bool  = True
        self._scope_title:   "str | None" = None
        self._top:           int   = _FM_DEFAULT_TOP
        self._info_on:       bool  = False
        self._embed_busy:    bool  = False
        self._last_results:  "list[dict]" = []
        self._status_msg:    str   = "waiting for connection…"

        # ── Title bar buttons ─────────────────────────────────────────
        self._title_label.hide()

        self._media_btn = _TbBtn(icon_name="video-camera-solid",
                                 text="  movie", icon_size=14,
                                 parent=self._title_bar)
        self._media_btn.setFont(theme.font_ui())
        self._media_btn.setStyleSheet(_TB_TEXT_BTN_SS)
        self._media_btn.clicked.connect(self._show_media_menu)
        self._tb_layout.insertWidget(0, self._media_btn, 0, Qt.AlignVCenter)

        self._scope_btn = _TbBtn(icon_name="search", text="  -- all",
                                 icon_size=14, parent=self._title_bar)
        self._scope_btn.setFont(theme.font_ui())
        self._scope_btn.setStyleSheet(_TB_TEXT_BTN_SS)
        self._scope_btn.clicked.connect(self._show_scope_menu)
        self._tb_layout.insertWidget(1, self._scope_btn, 0, Qt.AlignVCenter)

        self._top_btn = _TbBtn(text=f"  {{{self._top}}}", icon_size=14,
                               parent=self._title_bar)
        self._top_btn.setFont(theme.font_ui())
        self._top_btn.setStyleSheet(_TB_TEXT_BTN_SS)
        self._top_btn.clicked.connect(self._show_top_menu)
        self._tb_layout.insertWidget(2, self._top_btn, 0, Qt.AlignVCenter)

        self._info_btn = _TbBtn(icon_name="info-circle", icon_size=14,
                                parent=self._title_bar)
        self._info_btn.setFixedSize(22, 22)
        self._info_btn.setStyleSheet(_TB_ICON_BTN_SS)
        self._info_btn.clicked.connect(self._toggle_info)
        self._tb_layout.insertWidget(3, self._info_btn, 0, Qt.AlignVCenter)
        self._update_info_icon()

        # ── Content text area ─────────────────────────────────────────
        self._body = QWidget(self)
        body = self._body
        body.setStyleSheet("background: transparent;")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(8, 6, 8, 6)
        blay.setSpacing(0)

        self._result_text = QTextEdit(body)
        self._result_text.setReadOnly(True)
        self._result_text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._result_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._result_text.setStyleSheet(
            f"background: transparent; color: {_NODE_TEXT_COLOR};"
            "border: none; font-size: 10px; font-family: monospace;"
        )
        self._result_text.setFont(theme.font_mono() if hasattr(theme, "font_mono") else theme.font_ui())
        blay.addWidget(self._result_text)
        self.content_layout().addWidget(body)

        self._thread: "QThread | None" = None
        self._worker: "_FrameMatchWorker | None" = None

        # ── Edge resize (same as FrameVectorNode) ─────────────────────
        self._handle_bl.hide()
        self._handle_br.hide()
        self._edge_handles: dict[str, _EdgeResizeHandle] = {}
        self._edge_resize_geom = None
        for edge in ("top", "bottom", "left", "right", "tl", "tr", "bl", "br"):
            h = _EdgeResizeHandle(edge, self)
            h.resize_started.connect(self._on_edge_resize_start)
            h.resize_dragged.connect(self._on_edge_resize_drag)
            h.resize_released.connect(self._on_edge_resize_end)
            self._edge_handles[edge] = h
        self._reposition_edge_handles()

        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._cleanup_thread)

        self._update_result_text()

    # ------------------------------------------------------------------
    # SyncNode API
    # ------------------------------------------------------------------

    def node_type(self) -> str:
        return "frame_match"

    def input_specs(self) -> dict:
        return {
            "vector": {"type": "vector", "shape": "triangle", "dimension": 512, "label": "[512]"},
        }

    def output_specs(self) -> dict:
        return {
            "matches": {
                "type":      "matches",
                "shape":     "object",
                "dimension": None,
                "count":     self._top,
                "label":     f"{{{self._top}}}",
            },
        }

    def state_dict(self) -> dict:
        d = super().state_dict()
        d["target_media"] = self._media_type
        d["scope_all"]    = self._scope_all
        d["scope_title"]  = self._scope_title
        d["top"]          = self._top
        d["info"]         = self._info_on
        return d

    # ------------------------------------------------------------------
    # Connection callbacks
    # ------------------------------------------------------------------

    def on_connected(self, source_node) -> None:
        self._source_node = source_node
        self._status_msg  = "connected, waiting for vector…"
        self._update_result_text()

    def on_disconnected(self) -> None:
        self._source_node = None
        self._embed_busy  = False
        self._status_msg  = "waiting for connection…"
        self._last_results = []
        self._update_result_text()

    # ------------------------------------------------------------------
    # Title bar interactions
    # ------------------------------------------------------------------

    def _toggle_info(self) -> None:
        self._info_on = not self._info_on
        self._update_info_icon()
        self._update_result_text()

    def _update_info_icon(self) -> None:
        if self._info_on:
            self._info_btn.set_icon("info-circle-solid", theme.TEXT)
        else:
            self._info_btn.set_icon("info-circle", theme.TEXT_DIM)

    def _show_media_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {theme.PANEL_BG}; color: {theme.TEXT};"
            f"  border: 1px solid {theme.UI_BORDER}; padding: 2px; }}"
            f"QMenu::item {{ padding: 4px 16px; }}"
            f"QMenu::item:selected {{ background: {theme.ACCENT}; }}"
        )
        for mt in ("movie", "gameplay"):
            act = menu.addAction(mt)
            act.setCheckable(True)
            act.setChecked(self._media_type == mt)
            act.setData(mt)
        btn = self._media_btn
        chosen = menu.exec_(btn.mapToGlobal(QPoint(0, btn.height())))
        if chosen and chosen.data() and chosen.data() != self._media_type:
            self._media_type  = chosen.data()
            self._media_btn.setText(f"  {self._media_type}")
            # Reset scope when media type changes
            self._scope_all   = True
            self._scope_title = None
            self._scope_btn.setText("  -- all")
            from services import sync_frame_match as _sfm
            _sfm._catalog_cache.clear()
            self._last_results = []
            self._update_result_text()

    def _show_top_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {theme.PANEL_BG}; color: {theme.TEXT};"
            f"  border: 1px solid {theme.UI_BORDER}; padding: 2px; }}"
            f"QMenu::item {{ padding: 4px 16px; }}"
            f"QMenu::item:selected {{ background: {theme.ACCENT}; }}"
        )
        for n in _FM_TOP_OPTIONS:
            act = menu.addAction(f"{{{n}}}")
            act.setCheckable(True)
            act.setChecked(self._top == n)
            act.setData(n)
        btn = self._top_btn
        chosen = menu.exec_(btn.mapToGlobal(QPoint(0, btn.height())))
        if chosen and chosen.data() is not None and chosen.data() != self._top:
            self._top = int(chosen.data())
            self._top_btn.setText(f"  {{{self._top}}}")
            # Repaint workspace so port label updates
            ws = self.parent()
            if hasattr(ws, "update"):
                ws.update()

    def _show_scope_menu(self) -> None:
        # ── Fetch titles ──────────────────────────────────────────────
        try:
            from tool import prefs as _p
            project_path = _p.get("path") or "."
        except Exception:
            project_path = "."
        try:
            from data.metadata import get_metadata
            entries = get_metadata(project_path, media_type=self._media_type)
        except Exception:
            entries = []
        sorted_entries = sorted(
            entries,
            key=lambda e: (e.get("title") or e.get("filename", "")).lower(),
        )

        # ── Styles ────────────────────────────────────────────────────
        _ss_frame  = (
            f"QFrame {{ background: {theme.PANEL_BG};"
            f"  border: 1px solid {theme.UI_BORDER}; }}"
        )
        _ss_filter = (
            f"QLineEdit {{ background: {theme.PANEL_BG}; color: {theme.TEXT};"
            f"  border: none; border-bottom: 1px solid {theme.UI_BORDER};"
            f"  padding: 4px 6px; font-size: 11px; }}"
        )
        _ss_list = (
            f"QListWidget {{ background: {theme.PANEL_BG}; color: {theme.TEXT};"
            f"  border: none; font-size: 11px; outline: none; }}"
            f"QListWidget::item {{ padding: 3px 8px; }}"
            f"QListWidget::item:selected, QListWidget::item:hover"
            f"  {{ background: {theme.ACCENT}; color: #ffffff; }}"
            f"QScrollBar:vertical {{ width: 8px; background: transparent; margin: 0; }}"
            f"QScrollBar::handle:vertical {{ background: {theme.UI_BORDER};"
            f"  border-radius: 3px; min-height: 20px; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            f"  {{ height: 0; }}"
        )

        # ── Build popup ───────────────────────────────────────────────
        popup = QFrame(None, Qt.Popup | Qt.FramelessWindowHint)
        popup.setStyleSheet(_ss_frame)
        popup.setAttribute(Qt.WA_DeleteOnClose)
        popup.setFixedWidth(260)

        vlay = QVBoxLayout(popup)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)

        filter_edit = QLineEdit(popup)
        filter_edit.setPlaceholderText("filter\u2026")
        filter_edit.setStyleSheet(_ss_filter)
        filter_edit.setFixedHeight(28)
        vlay.addWidget(filter_edit)

        lst = QListWidget(popup)
        lst.setStyleSheet(_ss_list)
        lst.setFrameShape(QFrame.NoFrame)
        lst.setUniformItemSizes(True)
        lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        vlay.addWidget(lst)

        # ── Populate ──────────────────────────────────────────────────
        all_item = QListWidgetItem("\u2014\u2014  all  \u2014\u2014")
        all_item.setData(Qt.UserRole, None)
        lst.addItem(all_item)
        for entry in sorted_entries:
            label    = entry.get("title") or entry.get("filename", "?")
            filename = entry.get("filename") or label
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, filename)
            lst.addItem(it)

        # Pre-select current value
        for i in range(lst.count()):
            it = lst.item(i)
            d  = it.data(Qt.UserRole)
            if (self._scope_all and d is None) or (
                    not self._scope_all and d == self._scope_title):
                lst.setCurrentItem(it)
                lst.scrollToItem(it)
                break

        # Constrain height to MAX_VISIBLE rows
        MAX_VISIBLE = 25
        row_h   = max(lst.sizeHintForRow(0), 22) if lst.count() else 22
        visible = min(lst.count(), MAX_VISIBLE)
        lst.setFixedHeight(visible * row_h)

        # ── Filter ────────────────────────────────────────────────────
        def _apply_filter(text: str) -> None:
            q = text.strip().lower()
            for i in range(lst.count()):
                it = lst.item(i)
                if it.data(Qt.UserRole) is None:   # "all" always visible
                    it.setHidden(False)
                    continue
                it.setHidden(bool(q) and q not in it.text().lower())
        filter_edit.textChanged.connect(_apply_filter)

        # ── Pick ──────────────────────────────────────────────────────
        def _pick(item: QListWidgetItem) -> None:
            data = item.data(Qt.UserRole)
            if data is None:
                self._scope_all   = True
                self._scope_title = None
                self._scope_btn.setText("  -- all")
            else:
                self._scope_all   = False
                self._scope_title = data
                short = item.text()
                if len(short) > 22:
                    short = short[:20] + "\u2026"
                self._scope_btn.setText(f"  {short}")
            from services import sync_frame_match as _sfm
            _sfm._catalog_cache.clear()
            popup.close()
        lst.itemClicked.connect(_pick)

        # ── Key handling in filter field ──────────────────────────────
        def _filter_key(event) -> None:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                for i in range(lst.count()):
                    it = lst.item(i)
                    if not it.isHidden():
                        _pick(it)
                        return
            elif event.key() == Qt.Key_Escape:
                popup.close()
            elif event.key() == Qt.Key_Down:
                lst.setFocus()
                if lst.currentRow() < lst.count() - 1:
                    lst.setCurrentRow(lst.currentRow() + 1)
            else:
                QLineEdit.keyPressEvent(filter_edit, event)
        filter_edit.keyPressEvent = _filter_key

        # ── Position below scope button ───────────────────────────────
        btn = self._scope_btn
        popup.move(btn.mapToGlobal(QPoint(0, btn.height())))
        popup.show()
        filter_edit.setFocus()

    # ------------------------------------------------------------------
    # Event-driven vector input
    # ------------------------------------------------------------------

    def receive_input(self, port_name: str, value, meta: dict | None = None) -> None:
        """Called when a connected FrameVectorNode emits a new vector."""
        if port_name != "vector":
            return
        if self._embed_busy:
            return  # drop while search is in progress
        vec  = value
        meta = meta or {}
        self._embed_busy = True
        self._status_msg = "matching…"
        self._update_result_text()

        try:
            from tool import prefs as _p
            project_path = _p.get("path") or "."
        except Exception:
            project_path = "."

        thread = QThread()
        worker = _CatalogSearchWorker(
            vec, project_path, self._media_type,
            self._scope_title, self._scope_all, self._top,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.results.connect(self._on_match_results)
        worker.error.connect(self._on_match_error)
        worker.results.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_match_results(self, hits: list) -> None:
        self._embed_busy  = False
        self._status_msg  = f"matched — {len(hits)} results"
        self._last_results = hits
        self._update_result_text()
        if hits:
            self.emit_output("matches", hits, {
                "type":         "matches",
                "count":        len(hits),
                "target_media": self._media_type,
                "scope":        self._scope_title or "all",
                "top":          self._top,
                "timestamp":    _time.time(),
            })

    def _on_match_error(self, msg: str) -> None:
        self._embed_busy = False
        self._status_msg = f"error: {msg}"
        self._last_results = []
        self._update_result_text()

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _update_result_text(self) -> None:
        lines = []
        if self._info_on:
            lines.append(f"status: {self._status_msg}")
            lines.append("")
        for item in self._last_results:
            rank  = item.get("rank", "?")
            score = item.get("score", 0.0)
            title = item.get("title", item.get("filename", "?"))
            shot  = item.get("shot_id", "")
            lines.append(f"{rank}. {score:.3f}  {title}")
            if shot:
                lines.append(f"   shot: {shot}")
            lines.append("")
        if not lines:
            # Nothing to show — at least give a bare status so the area isn't blank
            lines.append(f"status: {self._status_msg}")
        self._result_text.setPlainText("\n".join(lines).rstrip())

    # ------------------------------------------------------------------
    # Chrome + resize
    # ------------------------------------------------------------------

    def _apply_title_chrome(self, show: bool) -> None:
        super()._apply_title_chrome(show)
        for btn in (self._media_btn, self._scope_btn, self._top_btn, self._info_btn):
            btn.setVisible(show)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_edge_handles()

    def _reposition_handles(self) -> None:
        """Override base: keep the 16:9-enforcing corner handles permanently hidden."""
        self._handle_bl.hide()
        self._handle_br.hide()

    def _reposition_edge_handles(self) -> None:
        w, h  = self.width(), self.height()
        th    = _EDGE_THICKNESS
        hs    = _HANDLE_SIZE
        self._edge_handles["top"].setGeometry(hs, 0, w - 2 * hs, th)
        self._edge_handles["bottom"].setGeometry(hs, h - th, w - 2 * hs, th)
        self._edge_handles["left"].setGeometry(0, hs, th, h - 2 * hs)
        self._edge_handles["right"].setGeometry(w - th, hs, th, h - 2 * hs)
        self._edge_handles["tl"].setGeometry(0, 0, hs, hs)
        self._edge_handles["tr"].setGeometry(w - hs, 0, hs, hs)
        self._edge_handles["bl"].setGeometry(0, h - hs, hs, hs)
        self._edge_handles["br"].setGeometry(w - hs, h - hs, hs, hs)

    def _on_edge_resize_start(self, edge: str) -> None:
        self._edge_resize_geom = self.geometry()

    def _on_edge_resize_end(self, edge: str) -> None:
        self._edge_resize_geom = None

    def _on_edge_resize_drag(self, edge: str, dx: int, dy: int) -> None:
        orig = self._edge_resize_geom
        if orig is None:
            return
        x, y = orig.left(), orig.top()
        w, h = orig.width(), orig.height()
        min_w, min_h = _FM_MIN_W, _NODE_TITLE_H + 80
        if edge in ("right", "tr", "br"):
            w = max(min_w, orig.width() + dx)
        if edge in ("left", "tl", "bl"):
            nw = max(min_w, orig.width() - dx)
            x  = orig.right() - nw + 1
            w  = nw
        if edge in ("bottom", "bl", "br"):
            h = max(min_h, orig.height() + dy)
        if edge in ("top", "tl", "tr"):
            nh = max(min_h, orig.height() - dy)
            y  = orig.bottom() - nh + 1
            h  = nh
        self.setGeometry(x, y, w, h)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup_thread(self) -> None:
        thread = self._thread
        self._thread = None
        self._worker = None
        self._embed_busy = False
        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(500)
            except RuntimeError:
                pass

    def _on_close(self) -> None:
        self._cleanup_thread()
        super()._on_close()


# ---------------------------------------------------------------------------
# FramesViewerNode — helper: timecode → seconds
# ---------------------------------------------------------------------------

def _fv2_tc_to_seconds(tc: str) -> float:
    """Convert 'HH:MM:SS.mmm' (or MM:SS or bare seconds) to float seconds."""
    if not tc:
        return 0.0
    try:
        parts = tc.replace(",", ".").split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60.0 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def _fv2_resolve_video(project_path: str, media_type: str, filename_or_stem: str) -> "Path | None":
    """Return the video Path for a given filename or bare stem, or None.

    The frame-matching catalog stores filenames without extensions (bare
    stems).  The metadata JSON stores full filenames including extension.
    This function:
      1. Tries ``filename_or_stem`` as a direct path (handles already-complete names).
      2. Looks up the stem in the project metadata to find the real filename.
      3. Falls back to scanning the video directory by stem if metadata has no match.
    """
    video_dir = Path(project_path) / "media" / "videos" / media_type
    stem = Path(filename_or_stem).stem   # no-op if already a stem; strips ext if present

    # 1. Direct path (exact filename including extension)
    direct = video_dir / filename_or_stem
    if direct.exists():
        return direct

    # 2. Metadata lookup — metadata stores full filenames with extension
    try:
        from data.metadata import load_json_metadata as _load_meta
        records = _load_meta(project_path, media_type)
        for r in records:
            fn = r.get("filename", "")
            if fn and Path(fn).stem == stem:
                candidate = video_dir / fn
                if candidate.exists():
                    return candidate
    except Exception:
        pass

    # 3. Fallback: scan video dir for any file whose stem matches
    if video_dir.exists():
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
        for p in video_dir.iterdir():
            if p.is_file() and p.stem == stem and p.suffix.lower() in _VIDEO_EXTS:
                return p

    return None


# ---------------------------------------------------------------------------
# FramesViewerNode — loop-mode single-shot video player
# ---------------------------------------------------------------------------

class _ShotLoopPlayer(QWidget):
    """Looping video player for one shot — used by FramesViewerNode loop mode.

    Plays the shot from ``start_time`` to ``end_time`` in a tight loop using
    ``cv2.VideoCapture`` + ``QTimer``, identical in pattern to
    ``ShotlistVisualizer``.  No audio, no background thread.
    """

    _INTERVAL_MS = 33   # ~30 fps tick

    def __init__(
        self,
        result:       dict,
        project_path: str,
        img_w:        int,
        img_h:        int,
        show_info:    bool,
        parent:       QWidget = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedWidth(img_w + 4)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")

        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(3)

        self._img_lbl = QLabel(self)
        self._img_lbl.setFixedSize(img_w, img_h)
        self._img_lbl.setAlignment(Qt.AlignCenter)
        self._img_lbl.setStyleSheet(
            "background: #1a1a1a; color: #444444;"
            "border: 1px solid #333333; font-size: 9px;"
        )
        self._img_lbl.setText("…")
        vlay.addWidget(self._img_lbl)

        if show_info:
            title = result.get("title") or result.get("filename") or ""
            if title:
                cap_lbl = QLabel(title, self)
                cap_lbl.setWordWrap(True)
                cap_lbl.setStyleSheet(
                    f"color: {_NODE_TEXT_COLOR}; background: transparent; font-size: 9px;"
                )
                cap_lbl.setFont(theme.font_ui())
                cap_lbl.setFixedWidth(img_w + 4)
                vlay.addWidget(cap_lbl)

        self._cap:         "object" = None   # cv2.VideoCapture or None
        self._start_frame: int      = 0
        self._dur_frames:  int      = 1
        self._fps:         float    = 30.0
        self._play_start:  float    = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(self._INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

        if not _HAS_CV2:
            self._img_lbl.setText("cv2 unavailable")
            return

        import cv2 as _cv2
        media_type  = result.get("media_type", "movie")
        filename    = result.get("filename",   "")
        if not filename:
            self._img_lbl.setText("no filename")
            return

        video_path = _fv2_resolve_video(project_path, media_type, filename)
        if video_path is None:
            print(f"[FramesViewer] video not found: {filename!r} in {project_path}/media/videos/{media_type}/")
            self._img_lbl.setText("file not found")
            return

        cap = _cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            self._img_lbl.setText("can't open")
            return

        fps   = cap.get(_cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(_cv2.CAP_PROP_FRAME_COUNT))

        # Primary: parse start/end frames from the shot_id  (format: "<media_id>@fSTART-fEND")
        # This is the authoritative source — the catalog always stores a shot_id
        # even when start_frame/end_frame/start_time/end_time fields are None.
        start_frame: int | None = None
        end_frame:   int | None = None
        shot_id = result.get("shot_id") or ""
        if shot_id:
            try:
                from data.media_id import parse_shot_id as _parse_shot_id
                _, start_frame, end_frame = _parse_shot_id(shot_id)
            except Exception:
                pass

        # Fallback 1: integer fields in the result dict
        if start_frame is None:
            try:
                v = result.get("start_frame")
                if v is not None:
                    start_frame = int(v)
            except (TypeError, ValueError):
                pass
        if end_frame is None:
            try:
                v = result.get("end_frame")
                if v is not None:
                    end_frame = int(v)
            except (TypeError, ValueError):
                pass

        # Fallback 2: timecode strings
        if start_frame is None:
            start_s     = _fv2_tc_to_seconds(result.get("start_time") or "")
            start_frame = max(0, int(start_s * fps))
        if end_frame is None or end_frame <= start_frame:
            end_s     = _fv2_tc_to_seconds(result.get("end_time") or "")
            end_frame = int(end_s * fps) if end_s > 0 else start_frame + int(3.0 * fps)

        if total > 0:
            end_frame = min(end_frame, total - 1)
        dur_frames = max(1, end_frame - start_frame)

        self._cap         = cap
        self._fps         = fps
        self._start_frame = start_frame
        self._dur_frames  = dur_frames

        # Show the first frame immediately using frame-accurate seeking
        cap.set(_cv2.CAP_PROP_POS_FRAMES, start_frame)
        ok, bgr = cap.read()
        if ok:
            self._show_bgr(bgr)

        self._play_start = _time.perf_counter()
        self._timer.start()

    # ------------------------------------------------------------------

    def _tick(self) -> None:
        if self._cap is None:
            return
        import cv2 as _cv2
        elapsed      = _time.perf_counter() - self._play_start
        offset       = int(elapsed * self._fps) % self._dur_frames
        target_frame = self._start_frame + offset
        self._cap.set(_cv2.CAP_PROP_POS_FRAMES, target_frame)
        ok, bgr = self._cap.read()
        if ok:
            self._show_bgr(bgr)

    def _show_bgr(self, bgr) -> None:
        import cv2 as _cv2
        rgb = _cv2.cvtColor(bgr, _cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        img  = QImage(rgb.tobytes(), w, h, rgb.strides[0], QImage.Format_RGB888)
        pix  = QPixmap.fromImage(img).scaled(
            self._img_lbl.width(), self._img_lbl.height(),
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self._img_lbl.setPixmap(pix)

    def stop(self) -> None:
        """Stop playback and release the VideoCapture handle."""
        self._timer.stop()
        if self._cap is not None:
            import cv2 as _cv2
            self._cap.release()
            self._cap = None



# ---------------------------------------------------------------------------
# FramesViewerNode — displays best-frame images from a match list
# ---------------------------------------------------------------------------

class FramesViewerNode(SyncNode):
    """Workspace node that renders best-frame images for received match results."""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__("Frames Viewer", parent)
        self._results:    list  = []
        self._meta:       dict  = {}
        self._status_msg: str   = "waiting for matches…"
        self._zoom:       float = 0.2   # tile width as fraction of native frame width
        self._input_count: int  = 0     # tracks live item count for port label
        self._info_on:    bool  = True  # when True, show movie titles below frames
        self._mode:       str   = "best"  # "best" | "loop"

        # ── Title bar buttons ─────────────────────────────────────────
        self._mode_btn = _TbBtn(
            text="  best", icon_size=14, parent=self._title_bar,
        )
        self._mode_btn.setFont(theme.font_ui())
        self._mode_btn.setStyleSheet(_TB_TEXT_BTN_SS)
        self._mode_btn.clicked.connect(self._toggle_mode)
        self._tb_layout.insertWidget(0, self._mode_btn, 0, Qt.AlignVCenter)

        self._zoom_btn = _TbBtn(
            text=f"  {int(self._zoom * 100)}%", icon_size=14,
            parent=self._title_bar,
        )
        self._zoom_btn.setFont(theme.font_ui())
        self._zoom_btn.setStyleSheet(_TB_TEXT_BTN_SS)
        self._zoom_btn.clicked.connect(self._show_zoom_menu)
        self._tb_layout.insertWidget(1, self._zoom_btn, 0, Qt.AlignVCenter)

        self._info_btn = _TbBtn(icon_name="info-circle", icon_size=14,
                                parent=self._title_bar)
        self._info_btn.setFixedSize(22, 22)
        self._info_btn.setStyleSheet(_TB_ICON_BTN_SS)
        self._info_btn.clicked.connect(self._toggle_info)
        self._tb_layout.insertWidget(2, self._info_btn, 0, Qt.AlignVCenter)
        self._update_info_icon()

        # ── Scroll area containing image tiles ────────────────────────
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 6px; background: #303030; }"
            "QScrollBar::handle:vertical { background: #606060; border-radius: 3px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )

        self._tiles_container = QWidget()
        self._tiles_container.setAttribute(Qt.WA_TranslucentBackground)
        self._tiles_container.setStyleSheet("background: transparent;")
        self._tiles_layout = QVBoxLayout(self._tiles_container)
        self._tiles_layout.setContentsMargins(0, 0, 0, 0)
        self._tiles_layout.setSpacing(6)
        self._tiles_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._tiles_container)
        self.content_layout().addWidget(self._scroll)

        # ── Edge resize handles ───────────────────────────────────────
        self._handle_bl.hide()
        self._handle_br.hide()
        self._edge_handles: dict[str, _EdgeResizeHandle] = {}
        self._edge_resize_geom = None
        for edge in ("top", "bottom", "left", "right", "tl", "tr", "bl", "br"):
            h = _EdgeResizeHandle(edge, self)
            h.resize_started.connect(self._on_edge_resize_start)
            h.resize_dragged.connect(self._on_edge_resize_drag)
            h.resize_released.connect(self._on_edge_resize_end)
            self._edge_handles[edge] = h
        self._reposition_edge_handles()

        self.resize(_FV2_DEFAULT_W, _FV2_DEFAULT_H)
        self._render_results()

    # ------------------------------------------------------------------
    # SyncNode API
    # ------------------------------------------------------------------

    def node_type(self) -> str:
        return "frames_viewer"

    def input_specs(self) -> dict:
        label = f"{{{self._input_count}}}" if self._input_count else "{}"
        return {
            "matches": {
                "type":      "matches",
                "shape":     "object",
                "dimension": None,
                "label":     label,
            },
        }

    def output_specs(self) -> dict:
        return {}

    def state_dict(self) -> dict:
        d = super().state_dict()
        d["zoom"] = self._zoom
        d["mode"] = self._mode
        return d

    def _toggle_mode(self) -> None:
        self._mode = "loop" if self._mode == "best" else "best"
        self._mode_btn.setText(f"  {self._mode}")
        self._render_results()

    def _show_zoom_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {theme.PANEL_BG}; color: {theme.TEXT};"
            f"  border: 1px solid {theme.UI_BORDER}; padding: 2px; }}"
            f"QMenu::item {{ padding: 4px 16px; }}"
            f"QMenu::item:selected {{ background: {theme.ACCENT}; }}"
        )
        for z in (0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5):
            label = f"{int(z * 100)}%"
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(abs(self._zoom - z) < 0.001)
            act.setData(z)
        btn = self._zoom_btn
        chosen = menu.exec_(btn.mapToGlobal(QPoint(0, btn.height())))
        if chosen and chosen.data() is not None:
            self._zoom = float(chosen.data())
            self._zoom_btn.setText(f"  {int(self._zoom * 100)}%")
            self._render_results()

    def _toggle_info(self) -> None:
        self._info_on = not self._info_on
        self._update_info_icon()
        self._render_results()

    def _update_info_icon(self) -> None:
        if self._info_on:
            self._info_btn.set_icon("info-circle-solid", theme.TEXT)
        else:
            self._info_btn.set_icon("info-circle", theme.TEXT_DIM)

    def _apply_title_chrome(self, show: bool) -> None:
        # Base class now iterates the full title-bar layout, so this is handled
        # automatically.  Keep override only to ensure _mode_btn is included
        # even if someone calls super() without the layout iteration.
        super()._apply_title_chrome(show)

    # ------------------------------------------------------------------
    # Connection callbacks
    # ------------------------------------------------------------------

    def on_connected(self, source_node) -> None:
        self._status_msg = "connected, waiting for match results…"
        self._render_results()

    def on_disconnected(self) -> None:
        self._results    = []
        self._meta       = {}
        self._status_msg = "waiting for matches…"
        self._render_results()

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    def receive_input(self, port_name: str, value, meta: dict | None = None) -> None:
        if port_name != "matches":
            return
        self._results    = value or []
        self._meta       = meta or {}
        self._input_count = len(self._results)
        self._status_msg = f"showing {self._input_count} matches"
        self._render_results()
        # Ask workspace to repaint so port label refreshes
        if isinstance(self.parent(), SyncWorkspace):
            self.parent().update()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_results(self) -> None:
        """Rebuild the tile list from current _results (single-column vertical stack)."""
        # Stop any running animations before clearing
        self._stop_anim_tiles()
        # Remove all existing tile widgets
        while self._tiles_layout.count():
            item = self._tiles_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self._results:
            lbl = QLabel(f"  {self._status_msg}", self._tiles_container)
            lbl.setStyleSheet(
                f"color: {_NODE_TEXT_COLOR}; background: transparent; font-size: 10px;"
            )
            lbl.setFont(theme.font_ui())
            self._tiles_layout.addWidget(lbl)
            return

        try:
            from tool import prefs as _p
            project_path = _p.get("path") or "."
        except Exception:
            project_path = "."

        img_w, img_h = self._tile_image_size()

        if self._mode == "loop":
            for result in self._results:
                player = _ShotLoopPlayer(
                    result, project_path, img_w, img_h,
                    show_info=self._info_on,
                    parent=self._tiles_container,
                )
                self._tiles_layout.addWidget(player)
        else:
            for result in self._results:
                tile = self._make_tile(result, project_path, img_w, img_h)
                self._tiles_layout.addWidget(tile)
        self._tiles_layout.addStretch()

    def _tile_image_size(self) -> tuple:
        """Compute tile size based on zoom % of native frame width (1920px reference)."""
        content_h = max(60, self.height() - _NODE_TITLE_H - 48)
        # tile width = zoom % of native frame width, capped at node width
        tile_w = int(_FRAME_NATURAL_W * self._zoom)
        tile_w = min(tile_w, max(60, self.width()))
        # height: maintain 16:9, capped at content height
        tile_h = int(tile_w * 9 / 16)
        if tile_h > content_h:
            tile_h = content_h
            tile_w = int(tile_h * 16 / 9)
        return tile_w, tile_h

    def _stop_anim_tiles(self) -> None:
        """Stop any running _ShotLoopPlayer instances before clearing tiles."""
        for i in range(self._tiles_layout.count()):
            item = self._tiles_layout.itemAt(i)
            if item and isinstance(item.widget(), _ShotLoopPlayer):
                item.widget().stop()

    def _make_tile(self, result: dict, project_path: str, img_w: int, img_h: int) -> QWidget:
        """Return a static best-frame tile widget."""
        tile = QWidget(self._tiles_container)
        tile.setFixedWidth(img_w + 4)
        tile.setAttribute(Qt.WA_TranslucentBackground)
        tile.setStyleSheet("background: transparent;")

        vlay = QVBoxLayout(tile)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(3)

        # ── Image label ───────────────────────────────────────────────
        img_lbl = QLabel(tile)
        img_lbl.setFixedSize(img_w, img_h)
        img_lbl.setAlignment(Qt.AlignCenter)
        img_lbl.setStyleSheet(
            "background: #1a1a1a; color: #666666;"
            "border: 1px solid #333333; font-size: 9px;"
        )

        path = self._resolve_frame_path(result, project_path)
        if path is not None:
            pix = QPixmap(str(path))
            if not pix.isNull():
                scaled = pix.scaled(
                    img_w, img_h,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
                img_lbl.setPixmap(scaled)
            else:
                img_lbl.setText("missing frame")
        else:
            img_lbl.setText("missing frame")
        vlay.addWidget(img_lbl)

        # ── Title label (only when info is on) ────────────────────────
        if self._info_on:
            title = result.get("title") or result.get("filename") or ""
            if title:
                cap_lbl = QLabel(title, tile)
                cap_lbl.setWordWrap(True)
                cap_lbl.setStyleSheet(
                    f"color: {_NODE_TEXT_COLOR}; background: transparent; font-size: 9px;"
                )
                cap_lbl.setFont(theme.font_ui())
                cap_lbl.setFixedWidth(img_w + 4)
                vlay.addWidget(cap_lbl)

        return tile

    def _resolve_frame_path(self, result: dict, project_path: str) -> "Path | None":
        """Resolve a best-frame PNG path from match result fields."""
        # 1. Explicit frame_path field (absolute or project-relative)
        fp = result.get("frame_path")
        if fp:
            p = Path(fp)
            if p.is_absolute() and p.exists():
                return p
            rp = Path(project_path) / p
            if rp.exists():
                return rp
        # 2. Derive from best_frame_path(project_path, media_type, filename, shot_id)
        media_type = result.get("media_type", "movie")
        filename   = result.get("filename",   "")
        shot_id    = result.get("shot_id",    "")
        if filename and shot_id and project_path:
            try:
                from services.frame_match import best_frame_path as _bfp
                p = _bfp(project_path, media_type, filename, shot_id)
                if p.exists():
                    return p
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------
    # Resize
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_edge_handles()
        if self._results:
            self._render_results()

    def _reposition_handles(self) -> None:
        """Override base: keep the 16:9-enforcing corner handles permanently hidden."""
        self._handle_bl.hide()
        self._handle_br.hide()

    def _reposition_edge_handles(self) -> None:
        w, h  = self.width(), self.height()
        th    = _EDGE_THICKNESS
        hs    = _HANDLE_SIZE
        self._edge_handles["top"].setGeometry(hs, 0, w - 2 * hs, th)
        self._edge_handles["bottom"].setGeometry(hs, h - th, w - 2 * hs, th)
        self._edge_handles["left"].setGeometry(0, hs, th, h - 2 * hs)
        self._edge_handles["right"].setGeometry(w - th, hs, th, h - 2 * hs)
        self._edge_handles["tl"].setGeometry(0, 0, hs, hs)
        self._edge_handles["tr"].setGeometry(w - hs, 0, hs, hs)
        self._edge_handles["bl"].setGeometry(0, h - hs, hs, hs)
        self._edge_handles["br"].setGeometry(w - hs, h - hs, hs, hs)

    def _on_edge_resize_start(self, edge: str) -> None:
        self._edge_resize_geom = self.geometry()

    def _on_edge_resize_end(self, edge: str) -> None:
        self._edge_resize_geom = None

    def _on_edge_resize_drag(self, edge: str, dx: int, dy: int) -> None:
        orig = self._edge_resize_geom
        if orig is None:
            return
        x, y = orig.left(), orig.top()
        w, h = orig.width(), orig.height()
        min_w, min_h = _FV2_MIN_W, _NODE_TITLE_H + 80
        if edge in ("right", "tr", "br"):
            w = max(min_w, orig.width() + dx)
        if edge in ("left", "tl", "bl"):
            nw = max(min_w, orig.width() - dx)
            x  = orig.right() - nw + 1
            w  = nw
        if edge in ("bottom", "bl", "br"):
            h = max(min_h, orig.height() + dy)
        if edge in ("top", "tl", "tr"):
            nh = max(min_h, orig.height() - dy)
            y  = orig.bottom() - nh + 1
            h  = nh
        self.setGeometry(x, y, w, h)


# ---------------------------------------------------------------------------
# ModuleItem — draggable palette entry with connector bumps
# ---------------------------------------------------------------------------

_MODULE_H           = 36
_MODULE_CORNER      = 6    # border-radius
_CONNECTOR_W        = 10   # horizontal extent of the triangle bump/socket
_CONNECTOR_H        = 14   # height of the triangle
_MODULE_BG          = "#ffffff"
_MODULE_BG_COLOR    = QColor("#ffffff")
_MODULE_SOCKET_COLOR = QColor("#404040")  # same as panel background


class ModuleItem(QWidget):
    """Draggable palette item drawn as a node-connector shape.

    Left side connector controlled by *input_shape*:
      None          — no connector
      "half_circle" — semicircle dome protruding left (image port)
      "triangle"    — triangular indent protruding left (vector port)
    Right side connector controlled by *output_shape* (same options, right).
    Background is white; icon and label use the workspace gray so they
    read as 'cut out' of the white shape.
    """

    def __init__(
        self,
        label:        str,
        item_type:    str,
        icon_name:    str | None = None,
        input_shape:  str | None = None,
        output_shape: str | None = None,
        parent:       QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._item_type       = item_type
        self._input_shape     = input_shape
        self._output_shape    = output_shape
        self._drag_start_pos: QPoint | None = None

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.OpenHandCursor)
        self.setFixedHeight(_MODULE_H)

        # Same content inset for every item so icons/labels always align.
        # Triangle connectors are drawn outside this content region.
        _pad = _CONNECTOR_W + 6
        layout = QHBoxLayout(self)
        layout.setContentsMargins(_pad, 0, _pad, 0)
        layout.setSpacing(6)

        icon_color = "#404040"  # panel bg — reads as cut-out on white
        self._icon_lbl = QLabel(self)
        icon_lbl = self._icon_lbl
        icon_lbl.setFixedSize(18, 18)
        icon_lbl.setStyleSheet("background: transparent;")
        self._icon_name = icon_name
        if icon_name:
            pix = _svg_icon(icon_name, 16, icon_color).pixmap(16, 16)
            if not pix.isNull():
                icon_lbl.setPixmap(pix)
        layout.addWidget(icon_lbl)

        text_lbl = QLabel(label, self)
        text_lbl.setStyleSheet(
            f"color: {_WORKSPACE_BG}; background: transparent; font-weight: 600;"
        )
        text_lbl.setFont(theme.font_ui())
        layout.addWidget(text_lbl)
        layout.addStretch()

    # ------------------------------------------------------------------
    # Custom shape drawing
    # ------------------------------------------------------------------

    def _build_path(self) -> QPainterPath:
        """White shape with shaped connector bumps determined by input_shape / output_shape.

        Connector space (cw px) is always reserved on both sides so that items
        without a connector still indent their text the same amount.
        """
        w, h = self.width(), self.height()
        r  = _MODULE_CORNER
        cw = _CONNECTOR_W
        ch = _CONNECTOR_H
        cy = h / 2

        lx = cw      # core left edge (connector space reserved)
        rx = w - cw  # core right edge

        path = QPainterPath()
        path.moveTo(lx + r, 0)
        path.lineTo(rx - r, 0)
        path.quadTo(rx, 0, rx, r)

        # ── Right output connector ────────────────────────────────────
        if self._output_shape == "half_circle":
            # Dome pointing right; Qt auto-lines from current pos to arc start
            path.arcTo(QRectF(rx - cw, cy - cw, 2 * cw, 2 * cw), 90, -180)
        elif self._output_shape == "triangle":
            path.lineTo(rx, cy - ch / 2)
            path.lineTo(w,  cy)
            path.lineTo(rx, cy + ch / 2)
        elif self._output_shape == "object":
            # Small square tab protruding right
            half = ch / 2 * 0.6
            path.lineTo(rx, cy - half)
            path.lineTo(w,  cy - half)
            path.lineTo(w,  cy + half)
            path.lineTo(rx, cy + half)

        path.lineTo(rx, h - r)
        path.quadTo(rx, h, rx - r, h)
        path.lineTo(lx + r, h)
        path.quadTo(lx, h, lx, h - r)

        # ── Left input connector ──────────────────────────────────────
        if self._input_shape == "half_circle":
            # Dome pointing left; draw from bottom to top via left side
            path.arcTo(QRectF(lx - cw, cy - cw, 2 * cw, 2 * cw), 270, -180)
        elif self._input_shape == "triangle":
            path.lineTo(lx, cy + ch / 2)
            path.lineTo(0,  cy)
            path.lineTo(lx, cy - ch / 2)
        elif self._input_shape == "object":
            # Small square indent on left side
            half = ch / 2 * 0.6
            path.lineTo(lx, cy + half)
            path.lineTo(0,  cy + half)
            path.lineTo(0,  cy - half)
            path.lineTo(lx, cy - half)

        path.lineTo(lx, r)
        path.quadTo(lx, 0, lx + r, 0)
        path.closeSubpath()
        return path

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillPath(self._build_path(), _MODULE_BG_COLOR)
        # For object-shaped connectors, cut a V-notch to make } / { silhouette
        if self._output_shape == "object" or self._input_shape == "object":
            cy    = self.height() / 2
            half  = _CONNECTOR_H * 0.5
            notch = half * 0.6
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.setPen(Qt.NoPen)
            if self._output_shape == "object":
                rx = self.width() - _CONNECTOR_W
                n_path = QPainterPath()
                n_path.moveTo(rx,          cy - notch)
                n_path.lineTo(rx + notch,  cy)
                n_path.lineTo(rx,          cy + notch)
                n_path.closeSubpath()
                painter.fillPath(n_path, QColor(0, 0, 0))
            if self._input_shape == "object":
                lx = _CONNECTOR_W
                n_path = QPainterPath()
                n_path.moveTo(lx,          cy - notch)
                n_path.lineTo(lx - notch,  cy)
                n_path.lineTo(lx,          cy + notch)
                n_path.closeSubpath()
                painter.fillPath(n_path, QColor(0, 0, 0))
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.end()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update()

    # ------------------------------------------------------------------
    # Drag
    # ------------------------------------------------------------------

    def enterEvent(self, event) -> None:
        if self._icon_name:
            pix = _svg_icon(self._icon_name, 16, theme.ACCENT).pixmap(16, 16)
            self._icon_lbl.setPixmap(pix)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._icon_name:
            pix = _svg_icon(self._icon_name, 16, "#404040").pixmap(16, 16)
            self._icon_lbl.setPixmap(pix)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not (event.buttons() & Qt.LeftButton) or self._drag_start_pos is None:
            super().mouseMoveEvent(event)
            return
        mime = QMimeData()
        mime.setData(_MIME_TYPE, self._item_type.encode())
        ghost = QPixmap(self.size())
        ghost.fill(QColor(_NODE_TITLE_BG))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(ghost)
        drag.setHotSpot(event.pos())
        self.setCursor(Qt.ClosedHandCursor)
        drag.exec_(Qt.CopyAction)
        self.setCursor(Qt.OpenHandCursor)
        self._drag_start_pos = None

    def mouseReleaseEvent(self, event) -> None:
        self._drag_start_pos = None
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# SyncPalettePanel — right-side panel
# ---------------------------------------------------------------------------

class SyncPalettePanel(QWidget):
    """Right-side panel with draggable module palette items."""

    # signal emitted when user clicks the Preferences button
    prefs_requested = pyqtSignal()

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(_PANEL_W)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 12, 8, 12)
        outer.setSpacing(8)

        items_widget = QWidget(self)
        items_widget.setAttribute(Qt.WA_TranslucentBackground)
        group_layout = QVBoxLayout(items_widget)
        group_layout.setContentsMargins(6, 6, 6, 6)
        group_layout.setSpacing(4)
        group_layout.addWidget(
            ModuleItem(
                label="Live Video",
                item_type="live_video",
                icon_name="video-camera-solid",
                input_shape=None,
                output_shape="half_circle",
                parent=items_widget,
            )
        )
        group_layout.addWidget(
            ModuleItem(
                label="Frame Vector",
                item_type="frame_vector",
                icon_name="calculator-solid",
                input_shape="half_circle",
                output_shape="triangle",
                parent=items_widget,
            )
        )
        group_layout.addWidget(
            ModuleItem(
                label="Frame Match",
                item_type="frame_match",
                icon_name="search",
                input_shape="triangle",
                output_shape="object",
                parent=items_widget,
            )
        )
        group_layout.addWidget(
            ModuleItem(
                label="Frames Viewer",
                item_type="frames_viewer",
                icon_name="media-image",
                input_shape="object",
                output_shape=None,
                parent=items_widget,
            )
        )

        outer.addWidget(items_widget)
        outer.addStretch()

        self._prefs_btn = QPushButton("Preferences", self)
        self._prefs_btn.setFlat(True)
        self._prefs_btn.setCursor(Qt.PointingHandCursor)
        self._prefs_btn.setFocusPolicy(Qt.NoFocus)
        self._prefs_btn.setFont(theme.font_ui())
        self._prefs_btn.setFixedHeight(_MODULE_H)
        # Equal 14px margin on both sides (outer 8 + inner items_widget 6).
        _btn_w = _PANEL_W - 14 - 14
        self._prefs_btn.setFixedWidth(_btn_w)
        self._prefs_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: #111111; color: #ffffff;"
            f"  border: none; border-radius: {_MODULE_CORNER}px;"
            f"  padding: 0; text-align: center;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: #ff00ff; color: #ffffff;"
            f"}}"
        )
        self._prefs_btn.clicked.connect(self.prefs_requested)
        # Wrap in row with 6px extra left indent so button starts at x=14 (= outer 8 + inner 6)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(6, 0, 0, 0)
        btn_row.setSpacing(0)
        btn_row.addWidget(self._prefs_btn)
        outer.addLayout(btn_row)

    _PANEL_COLOR = QColor("#404040")

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self._PANEL_COLOR)
        p.end()


# ---------------------------------------------------------------------------
# SyncPreferencesDialog — editable colour swatches
# ---------------------------------------------------------------------------

class _ColorPickBtn(QPushButton):
    """Plain button that shows a hex label and opens the standard QColorDialog."""
    changed = pyqtSignal(QColor)

    _SS = (
        "QPushButton {"
        "  background: transparent; color: #ffffff;"
        "  border: 1px solid #666666; border-radius: 3px; padding: 1px 8px;"
        "}"
        "QPushButton:hover { border-color: #ffffff; color: #ff00ff; }"
        "QPushButton:focus { outline: none; }"
    )

    def __init__(self, color: QColor, allow_alpha: bool = False,
                 parent: QWidget = None) -> None:
        super().__init__(parent)
        self._color       = QColor(color)
        self._allow_alpha = allow_alpha
        self.setFixedHeight(22)
        self.setMinimumWidth(90)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(self._SS)
        self._refresh()
        self.clicked.connect(self._pick)

    def color(self) -> QColor:
        return QColor(self._color)

    def set_color(self, c: QColor) -> None:
        self._color = QColor(c)
        self._refresh()

    def _refresh(self) -> None:
        if self._allow_alpha and self._color.alpha() < 255:
            self.setText(self._color.name(QColor.HexArgb))
        else:
            self.setText(self._color.name())

    def _pick(self) -> None:
        opts = QColorDialog.ColorDialogOptions()
        if self._allow_alpha:
            opts |= QColorDialog.ShowAlphaChannel
        c = QColorDialog.getColor(self._color, self, options=opts)
        if c.isValid():
            self._color = c
            self._refresh()
            self.changed.emit(c)


# Default colour values (used by the Defaults button)
_DEFAULT_WORKSPACE_BG = "#808080"
_DEFAULT_NODE_BG      = "#404040"
_DEFAULT_NODE_TEXT    = "#ffffff"
_DEFAULT_STAR_A       = "#00ff00"
_DEFAULT_STAR_B       = "#ff0000"


class SyncPreferencesDialog(QDialog):
    """Small modal dialog for editing Sync Visualizer colour preferences."""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sync Visualizer \u2014 Preferences")
        self.setModal(False)
        self.setFixedSize(360, 240)
        self.setStyleSheet(
            f"QDialog {{ background: {theme.PANEL_BG}; }}"
            f"QLabel   {{ color: {theme.TEXT}; background: transparent; }}"
        )

        _btn_ss = (
            f"QPushButton {{"
            f"  background: transparent; color: {theme.TEXT};"
            f"  border: 1px solid {theme.UI_BORDER}; padding: 2px 12px; border-radius: 3px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba(40,40,40,200); color: {theme.ACCENT};"
            f"}}"
        )

        grid = QGridLayout(self)
        grid.setContentsMargins(16, 16, 16, 16)
        grid.setSpacing(10)
        grid.setColumnStretch(1, 1)

        def _row(row: int, label: str, btn: _ColorPickBtn) -> None:
            lbl = QLabel(label, self)
            lbl.setFont(theme.font_ui())
            grid.addWidget(lbl, row, 0, Qt.AlignVCenter)
            grid.addWidget(btn, row, 1, Qt.AlignLeft | Qt.AlignVCenter)

        self._ws_btn = _ColorPickBtn(QColor(_WORKSPACE_BG), allow_alpha=False, parent=self)
        self._nd_btn = _ColorPickBtn(QColor(_NODE_BODY_BG),  allow_alpha=True,  parent=self)
        self._tx_btn = _ColorPickBtn(QColor(_NODE_TEXT_COLOR), allow_alpha=False, parent=self)
        self._sa_btn = _ColorPickBtn(_STAR_COLOR_A,           allow_alpha=False, parent=self)
        self._sb_btn = _ColorPickBtn(_STAR_COLOR_B,           allow_alpha=False, parent=self)

        _row(0, "Workspace background",     self._ws_btn)
        _row(1, "Module window background", self._nd_btn)
        _row(2, "Module text colour",       self._tx_btn)
        _row(3, "Star colour A (inner)",    self._sa_btn)
        _row(4, "Star colour B (outer)",    self._sb_btn)

        apply_btn    = QPushButton("Apply",    self)
        defaults_btn = QPushButton("Defaults", self)
        close_btn    = QPushButton("Close",    self)
        for b in (apply_btn, defaults_btn, close_btn):
            b.setStyleSheet(_btn_ss)
            b.setFocusPolicy(Qt.NoFocus)
        apply_btn.clicked.connect(self._apply)
        defaults_btn.clicked.connect(self._reset_defaults)
        close_btn.clicked.connect(self.close)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        btn_row.addWidget(defaults_btn)
        btn_row.addWidget(apply_btn)
        btn_row.addWidget(close_btn)
        grid.addLayout(btn_row, 5, 0, 1, 2)

    def _apply(self) -> None:
        from tool import prefs as _prefs
        _prefs.set(_PREFS_WORKSPACE_BG, self._ws_btn.color().name())
        _prefs.set(_PREFS_NODE_BG,      self._nd_btn.color().name(QColor.HexArgb))
        _prefs.set(_PREFS_NODE_TEXT,    self._tx_btn.color().name())
        _prefs.set(_PREFS_STAR_A,       self._sa_btn.color().name())
        _prefs.set(_PREFS_STAR_B,       self._sb_btn.color().name())
        _apply_color_prefs()

    def _reset_defaults(self) -> None:
        self._ws_btn.set_color(QColor(_DEFAULT_WORKSPACE_BG))
        self._nd_btn.set_color(QColor(_DEFAULT_NODE_BG))
        self._tx_btn.set_color(QColor(_DEFAULT_NODE_TEXT))
        self._sa_btn.set_color(QColor(_DEFAULT_STAR_A))
        self._sb_btn.set_color(QColor(_DEFAULT_STAR_B))

    def load_current(self) -> None:
        """Refresh buttons from current globals (call before showing)."""
        self._ws_btn.set_color(QColor(_WORKSPACE_BG))
        self._nd_btn.set_color(QColor(_NODE_BODY_BG))
        self._tx_btn.set_color(QColor(_NODE_TEXT_COLOR))
        self._sa_btn.set_color(QColor(_STAR_COLOR_A))
        self._sb_btn.set_color(QColor(_STAR_COLOR_B))


def _apply_color_prefs() -> None:
    """Read saved colour prefs and update module-level globals + live widgets."""
    global _WORKSPACE_BG, _NODE_BODY_BG, _NODE_TEXT_COLOR, _STAR_COLOR_A, _STAR_COLOR_B
    from tool import prefs as _prefs
    ws_bg = _prefs.get(_PREFS_WORKSPACE_BG)
    nd_bg = _prefs.get(_PREFS_NODE_BG)
    tx    = _prefs.get(_PREFS_NODE_TEXT)
    sa    = _prefs.get(_PREFS_STAR_A)
    sb    = _prefs.get(_PREFS_STAR_B)
    if ws_bg and QColor(ws_bg).isValid():
        _WORKSPACE_BG = ws_bg
    if nd_bg and QColor(nd_bg).isValid():
        _NODE_BODY_BG = nd_bg
    if tx and QColor(tx).isValid():
        _NODE_TEXT_COLOR = tx
    if sa and QColor(sa).isValid():
        _STAR_COLOR_A = QColor(sa)
    if sb and QColor(sb).isValid():
        _STAR_COLOR_B = QColor(sb)
    # Apply to all live workspace/node widgets
    app = QApplication.instance()
    if app is None:
        return
    for win in app.topLevelWidgets():
        if not hasattr(win, '_workspace'):
            continue
        ws = win._workspace
        ws.setStyleSheet(f"background: {_WORKSPACE_BG};")
        ws.update()
        for node in ws.nodes():
            if hasattr(node, '_content'):
                node._content.setStyleSheet(f"background: {_NODE_BODY_BG};")
                node._content.update()
            # Update text colour in FrameVectorNode
            if hasattr(node, '_vec_text'):
                node._vec_text.setStyleSheet(
                    f"background: transparent; color: {_NODE_TEXT_COLOR};"
                    f"border: none; font-size: 10px;"
                )


# ---------------------------------------------------------------------------
# SyncVisualizerWindow — main window
# ---------------------------------------------------------------------------

# Prefs key for QByteArray geometry blob (saveGeometry / restoreGeometry)
_PREFS_GEOM_DATA = "sync_visualizer_geometry_data"  # list[int] — bytes of QByteArray


class SyncVisualizerWindow(QMainWindow):
    """Main window for the Sync Visualizer.

    Keyboard:
      h  — toggle right panel + node chrome (presentation mode)
      f  — toggle fullscreen on the current monitor (edge-to-edge)
      Esc / Ctrl+Q / Ctrl+W — close

    Geometry is persisted via Qt's saveGeometry()/restoreGeometry(), which
    handles the X11 titlebar-offset issue and fullscreen/normal transitions
    correctly on all platforms.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(_WINDOW_TITLE)
        self.resize(_WINDOW_W, _WINDOW_H)

        self._chrome_hidden = False
        self._fs_active     = False
        self._windowed_geom: QRect | None = None

        # ── Central layout ─────────────────────────────────────────────────
        central = QWidget(self)
        hlayout = QHBoxLayout(central)
        hlayout.setContentsMargins(0, 0, 0, 0)
        hlayout.setSpacing(0)

        self._workspace = SyncWorkspace(central)
        hlayout.addWidget(self._workspace, 1)

        self._sep = QFrame(central)
        self._sep.setFrameShape(QFrame.VLine)
        self._sep.setStyleSheet(f"color: {theme.UI_BORDER};")
        self._sep.setFixedWidth(1)
        hlayout.addWidget(self._sep)

        self._palette = SyncPalettePanel(central)
        hlayout.addWidget(self._palette)
        self._palette.prefs_requested.connect(self._open_prefs)
        self._prefs_dialog: SyncPreferencesDialog | None = None

        self.setCentralWidget(central)
        self._restore_state()

    # ------------------------------------------------------------------
    # Track windowed geometry (only when not in fullscreen mode)
    # ------------------------------------------------------------------

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if not self._fs_active:
            self._windowed_geom = QRect(self.geometry())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._fs_active:
            self._windowed_geom = QRect(self.geometry())

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        from tool import prefs as _prefs
        g = self._windowed_geom if self._windowed_geom is not None else self.geometry()
        _prefs.set(_PREFS_GEOM,    [g.x(), g.y(), g.width(), g.height()])
        _prefs.set(_PREFS_FULLSCR, self._fs_active)
        _prefs.set(_PREFS_PANEL,   self._palette.isVisible())
        _prefs.set(_PREFS_NODES,   self._workspace.nodes_state())
        _prefs.set(_PREFS_CONNS,   self._workspace.connections_state())

    def _restore_state(self) -> None:
        from tool import prefs as _prefs
        _apply_color_prefs()

        geom = _prefs.get(_PREFS_GEOM)
        if isinstance(geom, (list, tuple)) and len(geom) == 4:
            x, y, w, h = (int(v) for v in geom)
            avail = QApplication.primaryScreen().availableGeometry()
            x = max(avail.left(), min(x, avail.right()  - 100))
            y = max(avail.top(),  min(y, avail.bottom() - 100))
            self.setGeometry(x, y, w, h)
        self._windowed_geom = QRect(self.geometry())

        nodes_state = _prefs.get(_PREFS_NODES)
        if isinstance(nodes_state, list):
            self._workspace.restore_nodes(nodes_state)

        conns_state = _prefs.get(_PREFS_CONNS)
        if isinstance(conns_state, list):
            self._workspace.restore_connections(conns_state)

        if _prefs.get(_PREFS_PANEL) is False:
            self._palette.setVisible(False)
            self._sep.setVisible(False)
            self._workspace.set_chrome_visible(False)
            self._chrome_hidden = True

        self._restore_fs = bool(_prefs.get(_PREFS_FULLSCR))

    def _apply_startup_fullscreen(self) -> None:
        if getattr(self, "_restore_fs", False):
            self._enter_fullscreen()

    def _open_prefs(self) -> None:
        if self._prefs_dialog is None:
            self._prefs_dialog = SyncPreferencesDialog(self)
        self._prefs_dialog.load_current()
        self._prefs_dialog.show()
        self._prefs_dialog.raise_()

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._save_state()
        _cleanup_audio_loopbacks()
        if self._fs_active:
            self.setWindowFlag(Qt.FramelessWindowHint, False)
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        k       = event.key()
        is_ctrl = bool(event.modifiers() & Qt.ControlModifier)

        if k == Qt.Key_H and not is_ctrl:
            self._toggle_chrome()
        elif k == Qt.Key_F and not is_ctrl:
            self._toggle_fullscreen()
        elif k == Qt.Key_Escape or (is_ctrl and k in (Qt.Key_Q, Qt.Key_W)):
            self.close()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # h — toggle chrome (panel + node title bars + resize handles)
    # ------------------------------------------------------------------

    def _toggle_chrome(self) -> None:
        self._chrome_hidden = not self._chrome_hidden
        self._palette.setVisible(not self._chrome_hidden)
        self._sep.setVisible(not self._chrome_hidden)
        self._workspace.set_chrome_visible(not self._chrome_hidden)

    # ------------------------------------------------------------------
    # f — FramelessWindowHint + setGeometry(screen) — reliable on X11/KWin
    # ------------------------------------------------------------------

    def _toggle_fullscreen(self) -> None:
        if self._fs_active:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self) -> None:
        self._windowed_geom = QRect(self.geometry())
        screen = QApplication.screenAt(self.geometry().center())
        if screen is None:
            screen = QApplication.primaryScreen()
        self._fs_active = True
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setGeometry(screen.geometry())
        self.show()

    def _exit_fullscreen(self) -> None:
        self._fs_active = False
        self.setWindowFlag(Qt.FramelessWindowHint, False)
        self.show()
        if self._windowed_geom is not None:
            self.setGeometry(self._windowed_geom)


# ---------------------------------------------------------------------------
# Public launcher
# ---------------------------------------------------------------------------

def run_visualizer() -> None:
    """Create QApplication (if needed) and open the Sync Visualizer."""
    import os as _os
    # Suppress OpenCV WARN-level stderr noise (V4L2 timeouts, metadata-node
    # open failures, etc.) — real errors still appear at ERROR level.
    _os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    from visualizers._window_helpers import raise_existing_window
    if raise_existing_window("sync"):
        return
    _cleanup_audio_loopbacks()  # clear any stale loops from crashed/previous runs
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    win = SyncVisualizerWindow()
    win.show()
    win._apply_startup_fullscreen()
    sys.exit(app.exec_())


if __name__ == "__main__":
    run_visualizer()

