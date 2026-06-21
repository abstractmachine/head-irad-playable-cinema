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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme

from PyQt5.QtCore import (
    QEvent,
    QMimeData,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QDrag,
    QIcon,
    QImage,
    QPainter,
    QPixmap,
    QPolygon,
)
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
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
_NODE_TITLE_BG   = "#404040"   # 25% gray node title bar
_NODE_BODY_BG    = "#bfbfbf"   # 75% gray node content area

_NODE_TITLE_H    = 26
_NODE_DEFAULT_W  = 480
_NODE_DEFAULT_H  = _NODE_TITLE_H + round(_NODE_DEFAULT_W * 9 / 16)   # 296
_NODE_MIN_W      = 240
_NODE_MIN_H      = _NODE_TITLE_H + round(_NODE_MIN_W * 9 / 16)

_HANDLE_SIZE     = 14   # px — corner resize hit zone

_VIDEO_DEVICE    = "/dev/video0"
_VIDEO_FPS_MS    = 33    # ~30 fps timer interval

_MIME_TYPE       = "application/x-crossing-sync-palette-item"

# Prefs keys
_PREFS_GEOM      = "sync_visualizer_geometry"       # [x, y, w, h]
_PREFS_FULLSCR   = "sync_visualizer_fullscreen"     # bool
_PREFS_PANEL     = "sync_visualizer_panel_visible"  # bool
_PREFS_NODES     = "sync_visualizer_nodes"          # list of dicts


# ---------------------------------------------------------------------------
# SVG icon helper
# ---------------------------------------------------------------------------

def _svg_icon(name: str, size: int = 16, color: str = "#ffffff") -> QIcon:
    """Load an iconoir SVG, recolour strokes/fills to *color*, return QIcon."""
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
        return QIcon(pix)
    return QIcon()


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


# ---------------------------------------------------------------------------
# LiveVideoWidget — low-latency /dev/video0 preview
# ---------------------------------------------------------------------------

class LiveVideoWidget(QLabel):
    """OpenCV + QTimer live preview.

    Reads one frame per ~33 ms tick on the main thread.  Sets V4L2 buffer
    size to 1 to minimise latency.  Scales the last captured frame to fill
    the widget on every resize so the node can be freely resized.

    Falls back to a "No live video signal" message when cv2 is unavailable
    or the device cannot be opened.
    """

    def __init__(self, device: str = _VIDEO_DEVICE, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._device        = device
        self._cap           = None
        self._timer         = None
        self._last_pixmap: QPixmap | None = None

        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(
            f"background: {theme.CANVAS_BG}; color: {theme.TEXT_DIM};"
        )
        self._try_open()

    def _try_open(self) -> None:
        if not _HAS_CV2:
            self.setText("No live video signal\n(cv2 not available)")
            return
        try:
            # V4L2 backend requires an integer index, not a string path.
            # Parse "/dev/videoN" → N; fall back to the string for non-standard paths.
            import re as _re
            _m = _re.search(r"(\d+)$", self._device)
            dev = int(_m.group(1)) if _m else self._device
            cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
            if not cap.isOpened():
                raise RuntimeError("device not opened")
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self._cap   = cap
            self._timer = QTimer(self)
            self._timer.setInterval(_VIDEO_FPS_MS)
            self._timer.timeout.connect(self._read_frame)
            self._timer.start()
        except Exception:
            self.setText("No live video signal")

    def _read_frame(self) -> None:
        if self._cap is None:
            return
        ret, frame = self._cap.read()
        if not ret:
            return
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch  = frame_rgb.shape
        img = QImage(frame_rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self._last_pixmap = QPixmap.fromImage(img)
        self._redisplay()

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
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None


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

        self._drag_global_start: QPoint | None = None
        self._drag_node_start:   QPoint | None = None
        self._resize_orig_geom                 = None   # QRect captured at drag start

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
            f"color: {theme.TEXT}; background: transparent;"
        )
        self._title_label.setFont(theme.font_ui())
        tb.addWidget(self._title_label, 0, Qt.AlignVCenter)
        tb.addStretch()

        self._close_btn = QPushButton("×", self._title_bar)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent; color: {theme.TEXT};"
            f"  border: none; font-size: 14px; padding: 0;"
            f"}}"
            f"QPushButton:hover {{ color: {theme.ACCENT}; }}"
        )
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
            "x": self.x(), "y": self.y(),
            "w": self.width(), "h": self.height(),
        }

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
        """Show or hide the title bar chrome (bg, label, close button)."""
        if show:
            self._title_bar.setStyleSheet(f"background: {_NODE_TITLE_BG};")
            self._title_label.setVisible(True)
            self._close_btn.setVisible(True)
        else:
            self._title_bar.setStyleSheet("background: transparent;")
            self._title_label.setVisible(False)
            self._close_btn.setVisible(False)

    # ------------------------------------------------------------------
    # Hover reveals title bar only in h-key hidden mode
    # ------------------------------------------------------------------

    def enterEvent(self, event) -> None:
        if not self._chrome_globally_visible:
            self._apply_title_chrome(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._chrome_globally_visible and self._drag_global_start is None:
            self._apply_title_chrome(False)
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

        # ── Title bar: camera icon + clickable device name ────────────────
        tb = self._tb_layout
        # Remove the auto-created empty title label; replace with our widgets
        tb.removeWidget(self._title_label)
        self._title_label.hide()

        cam_icon = _svg_icon("video-camera", 14, theme.TEXT)
        self._cam_icon_btn = QPushButton(self._title_bar)
        self._cam_icon_btn.setIcon(cam_icon)
        self._cam_icon_btn.setIconSize(QSize(14, 14))
        self._cam_icon_btn.setFixedSize(22, 22)
        self._cam_icon_btn.setFlat(True)
        self._cam_icon_btn.setCursor(Qt.PointingHandCursor)
        self._cam_icon_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; padding: 0; }"
            "QPushButton:hover { background: rgba(255,255,255,30); border-radius: 3px; }"
        )
        self._cam_icon_btn.clicked.connect(self._show_device_menu)
        tb.insertWidget(0, self._cam_icon_btn, 0, Qt.AlignVCenter)

        label = self._device if self._device else "<select-input>"
        self._dev_name_btn = QPushButton(label, self._title_bar)
        self._dev_name_btn.setFlat(True)
        self._dev_name_btn.setCursor(Qt.PointingHandCursor)
        self._dev_name_btn.setFont(theme.font_ui())
        self._dev_name_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {theme.TEXT};"
            f"  border: none; padding: 0 4px; text-align: left; }}"
            f"QPushButton:hover {{ color: {theme.ACCENT}; }}"
        )
        self._dev_name_btn.clicked.connect(self._show_device_menu)
        tb.insertWidget(1, self._dev_name_btn, 0, Qt.AlignVCenter)

        # Sound button — inserted just before the close button (last item)
        self._audio_module_id: int | None = None
        self._snd_btn = QPushButton(self._title_bar)
        self._snd_btn.setIcon(_svg_icon("sound-off", 14, theme.TEXT))
        self._snd_btn.setIconSize(QSize(14, 14))
        self._snd_btn.setFixedSize(22, 22)
        self._snd_btn.setFlat(True)
        self._snd_btn.setCursor(Qt.PointingHandCursor)
        self._snd_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; padding: 0; }"
            "QPushButton:hover { background: rgba(255,255,255,30); border-radius: 3px; }"
        )
        self._snd_btn.clicked.connect(self._toggle_audio)
        # tb already has: [cam, dev_name, stretch, close] — insert before close
        tb.insertWidget(tb.count() - 1, self._snd_btn, 0, Qt.AlignVCenter)

        # ── Video widget (only if a device was given) ─────────────────────
        self._video = LiveVideoWidget(self._device, self) if self._device else None
        if self._video:
            self.content_layout().addWidget(self._video)

    def node_type(self) -> str:
        return "live_video"

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
        self._cam_icon_btn.setVisible(show)
        self._snd_btn.setVisible(show and bool(self._device))
        self._dev_name_btn.setVisible(show)

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
        btn = self._dev_name_btn
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
            self._snd_btn.setIcon(_svg_icon("sound-off", 14, theme.TEXT))
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
                self._snd_btn.setIcon(_svg_icon("sound-high-solid", 14, theme.TEXT))
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
            self._snd_btn.setIcon(_svg_icon("sound-off", 14, theme.TEXT))
        if self._video is not None:
            self._video.stop()
            self.content_layout().removeWidget(self._video)
            self._video.deleteLater()
        self._device = device
        self._dev_name_btn.setText(device)
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
    """Absolute-positioned canvas that accepts dropped palette items as nodes."""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setStyleSheet(f"background: {_WORKSPACE_BG};")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._nodes: list[SyncNode] = []
        self._chrome_visible: bool  = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def nodes(self) -> list[SyncNode]:
        return list(self._nodes)

    def nodes_state(self) -> list[dict]:
        return [n.state_dict() for n in self._nodes]

    def restore_nodes(self, states: list[dict]) -> None:
        for s in states:
            if not isinstance(s, dict):
                continue
            if s.get("type") == "live_video":
                device = s.get("device", _VIDEO_DEVICE)
                w = max(_NODE_MIN_W, int(s.get("w", _NODE_DEFAULT_W)))
                h = _NODE_TITLE_H + round(w * 9 / 16)
                node = self._make_live_video_node(device)
                node.setGeometry(int(s.get("x", 0)), int(s.get("y", 0)), w, h)
                node.show()
                if s.get("audio") and device:
                    node._toggle_audio()

    def set_chrome_visible(self, visible: bool) -> None:
        self._chrome_visible = visible
        for node in self._nodes:
            node.set_chrome_visible(visible)

    def used_devices(self) -> set:
        """Return the set of device paths currently claimed by nodes (excludes unselected)."""
        return {n._device for n in self._nodes if isinstance(n, LiveVideoNode) and n._device}

    # ------------------------------------------------------------------
    # Drag-and-drop
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
            # Pick the first free device; fall back to None (shows <select-input>)
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
        event.acceptProposedAction()

    # ------------------------------------------------------------------
    # Node factory
    # ------------------------------------------------------------------

    def _make_live_video_node(self, device: str) -> LiveVideoNode:
        node = LiveVideoNode(device=device, parent=self)
        node.closed.connect(self._on_node_closed)
        node.set_chrome_visible(self._chrome_visible)
        self._nodes.append(node)
        return node

    def _on_node_closed(self, node: SyncNode) -> None:
        if node in self._nodes:
            self._nodes.remove(node)


# ---------------------------------------------------------------------------
# PaletteItem — draggable palette entry
# ---------------------------------------------------------------------------

class PaletteItem(QWidget):
    """One draggable item in the right-side palette panel."""

    def __init__(
        self,
        label:     str,
        item_type: str,
        icon_name: str | None = None,
        parent:    QWidget    = None,
    ) -> None:
        super().__init__(parent)
        self._item_type = item_type
        self._drag_start_pos: QPoint | None = None

        self.setCursor(Qt.OpenHandCursor)
        self.setFixedHeight(36)
        self.setStyleSheet(
            f"QWidget {{ background: {theme.INPUT_BG}; border-radius: 4px; }}"
            f"QWidget:hover {{ background: {theme.BTN_HOVER}; }}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(6)

        icon_lbl = QLabel(self)
        icon_lbl.setFixedSize(18, 18)
        icon_lbl.setStyleSheet("background: transparent;")
        if icon_name:
            icon = _svg_icon(icon_name, 16, "#ffffff")
            if not icon.isNull():
                icon_lbl.setPixmap(icon.pixmap(16, 16))
            else:
                icon_lbl.setText("▶")
                icon_lbl.setAlignment(Qt.AlignCenter)
        else:
            icon_lbl.setText("▶")
            icon_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon_lbl)

        text_lbl = QLabel(label, self)
        text_lbl.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        text_lbl.setFont(theme.font_ui())
        layout.addWidget(text_lbl)
        layout.addStretch()

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
    """Right-side panel with draggable input palette items."""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(_PANEL_W)
        self.setStyleSheet(f"background: {theme.PANEL_BG};")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 12, 8, 12)
        outer.setSpacing(8)

        inputs_group = QGroupBox("Inputs", self)
        inputs_group.setStyleSheet(
            f"QGroupBox {{"
            f"  border: 1px solid {theme.UI_BORDER};"
            f"  border-radius: 4px;"
            f"  margin-top: 10px;"
            f"  color: {theme.TEXT};"
            f"  background: transparent;"
            f"}}"
            f"QGroupBox::title {{"
            f"  subcontrol-origin: margin; left: 8px; padding: 0 4px;"
            f"}}"
        )
        group_layout = QVBoxLayout(inputs_group)
        group_layout.setContentsMargins(6, 6, 6, 6)
        group_layout.setSpacing(4)
        group_layout.addWidget(
            PaletteItem(
                label="Live Video",
                item_type="live_video",
                icon_name="video-camera",
                parent=inputs_group,
            )
        )

        outer.addWidget(inputs_group)
        outer.addStretch()


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

    def _restore_state(self) -> None:
        from tool import prefs as _prefs

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

        if _prefs.get(_PREFS_PANEL) is False:
            self._palette.setVisible(False)
            self._sep.setVisible(False)
            self._workspace.set_chrome_visible(False)
            self._chrome_hidden = True

        self._restore_fs = bool(_prefs.get(_PREFS_FULLSCR))

    def _apply_startup_fullscreen(self) -> None:
        if getattr(self, "_restore_fs", False):
            self._enter_fullscreen()

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

