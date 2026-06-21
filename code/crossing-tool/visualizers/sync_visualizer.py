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
    QObject,
    QPoint,
    QPointF,
    QRect,
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
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
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

_VIDEO_DEVICE    = "/dev/video0"
_VIDEO_FPS_MS    = 33    # ~30 fps timer interval

_MIME_TYPE       = "application/x-crossing-sync-palette-item"

# Prefs keys
_PREFS_GEOM      = "sync_visualizer_geometry"       # [x, y, w, h]
_PREFS_FULLSCR   = "sync_visualizer_fullscreen"     # bool
_PREFS_PANEL     = "sync_visualizer_panel_visible"  # bool
_PREFS_NODES     = "sync_visualizer_nodes"          # list of dicts
_PREFS_CONNS     = "sync_visualizer_connections"    # list of dicts

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
    "  background: rgba(40,40,40,200); border-radius: 3px;"
    "}"
)
_TB_TEXT_BTN_SS = (
    "QPushButton {"
    "  background: transparent; color: #ffffff;"
    "  border: none; padding: 0 4px;"
    "}"
    "QPushButton:hover {"
    "  background: rgba(40,40,40,200); border-radius: 3px; color: #ff00ff;"
    "}"
)
# Close button has an extra font-size rule
_TB_CLOSE_BTN_SS = (
    "QPushButton {"
    "  background: transparent; color: #ffffff;"
    "  border: none; font-size: 14px; padding: 0;"
    "}"
    "QPushButton:hover {"
    "  background: rgba(40,40,40,200); border-radius: 3px; color: #ff00ff;"
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
        self._refresh_icon("#ff00ff")
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
        cursor = Qt.SizeVerCursor if edge in ("top", "bottom") else Qt.SizeHorCursor
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
        self._device          = device
        self._cap             = None
        self._timer           = None
        self._last_pixmap: QPixmap | None = None
        self._last_frame_rgb  = None   # np.ndarray HxWx3 uint8, or None

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
        self._last_frame_rgb = frame_rgb          # store for consumers
        h, w, ch  = frame_rgb.shape
        img = QImage(frame_rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self._last_pixmap = QPixmap.fromImage(img)
        self._redisplay()

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

    def output_ports(self) -> list[str]:
        """Return list of output port names (e.g. ['image'])."""
        return []

    def input_ports(self) -> list[str]:
        """Return list of input port names."""
        return []

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
        self._cam_btn = _TbBtn(icon_name="video-camera", text=f"  {label}",
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
        tb.insertWidget(tb.count() - 1, self._snd_btn, 0, Qt.AlignVCenter)

        self._audio_module_id: int | None = None

        # ── Video widget (only if a device was given) ─────────────────────
        self._video = LiveVideoWidget(self._device, self) if self._device else None
        if self._video:
            self.content_layout().addWidget(self._video)

    def node_type(self) -> str:
        return "live_video"

    def output_ports(self) -> list[str]:
        return ["image"]

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
                    # Disconnect any existing cable from this output
                    self._remove_connections_for(node.node_id, port, is_output=True)
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
                    self._remove_connections_for(node.node_id, port, is_output=False)
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
            self._try_complete_connection(event.pos())
        self._drag_src_node = None
        self._drag_src_port = None
        self._drag_cur_pos  = None
        self._drag_hover_node = None
        self._drag_hover_port = None
        self.update()

    def _try_complete_connection(self, pos: QPoint) -> None:
        src = self._drag_src_node
        src_port = self._drag_src_port
        is_output = self._drag_src_is_output
        for node in self._nodes:
            if node is src or not node.isVisible():
                continue
            if is_output:
                # src is output → look for compatible input
                for port in node.input_ports():
                    p = node.input_port_pos(port)
                    if (pos - p).manhattanLength() <= _PORT_SIZE + 8:
                        if self._types_compatible(src, src_port, node, port):
                            self._remove_connections_for(node.node_id, port, False)
                            self._add_connection(src, src_port, node, port)
                        return
            else:
                # src is input → look for compatible output
                for port in node.output_ports():
                    p = node.output_port_pos(port)
                    if (pos - p).manhattanLength() <= _PORT_SIZE + 8:
                        if self._types_compatible(node, port, src, src_port):
                            self._remove_connections_for(src.node_id, src_port, False)
                            self._add_connection(node, port, src, src_port)
                        return

    def _types_compatible(self, src_node, src_port, tgt_node, tgt_port) -> bool:
        # For now: image→image only
        return src_port == "image" and tgt_port == "image"

    def _add_connection(self, src: SyncNode, src_port: str,
                        tgt: SyncNode, tgt_port: str) -> None:
        self._connections.append({
            "source_node": src.node_id, "source_port": src_port,
            "target_node": tgt.node_id, "target_port": tgt_port,
        })
        # Notify FrameVectorNode it has a new source
        if isinstance(tgt, FrameVectorNode):
            tgt.on_connected(src)
        src.update()
        tgt.update()
        self.update()

    def _remove_connections_for(self, node_id: int, port: str, is_output: bool) -> None:
        id_map = {n.node_id: n for n in self._nodes}
        kept = []
        for c in self._connections:
            if is_output and c["source_node"] == node_id and c["source_port"] == port:
                tgt = id_map.get(c["target_node"])
                if isinstance(tgt, FrameVectorNode):
                    tgt.on_disconnected()
            elif not is_output and c["target_node"] == node_id and c["target_port"] == port:
                tgt = id_map.get(c["target_node"])
                if isinstance(tgt, FrameVectorNode):
                    tgt.on_disconnected()
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

        # Draw port triangles for all nodes — always visible, positioned OUTSIDE
        # node bounds so clicks on them reach this workspace mousePressEvent.
        p.setPen(Qt.NoPen)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = _PORT_SIZE
        for node in self._nodes:
            if not node.isVisible():
                continue
            cy = node.y() + node.height() // 2
            for port in node.output_ports():
                bx = node.x() + node.width()   # base at right edge of node
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
                p.drawPolygon(QPolygon([
                    QPoint(bx, cy - s // 2),
                    QPoint(tx, cy),
                    QPoint(bx, cy + s // 2),
                ]))
            for port in node.input_ports():
                bx = node.x()    # base at left edge of node
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
                p.drawPolygon(QPolygon([
                    QPoint(bx, cy - s // 2),
                    QPoint(tx, cy),
                    QPoint(bx, cy + s // 2),
                ]))

        p.end()

    def _draw_cable(self, painter: QPainter, p1: QPoint, p2: QPoint) -> None:
        """Draw a bezier cable from p1 (output) to p2 (input).

        Control points always exit/enter horizontally so the cable visibly
        leaves the output port to the right and arrives at the input port
        from the left.  When the input is to the left of the output the
        tension is increased so the cable loops out before doubling back.
        """
        dx = p2.x() - p1.x()
        # Minimum tension ensures a visible rightward exit even when looping back
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
            pen.setWidthF(0.75)
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

        self._scan_btn = _TbBtn(icon_name="calculator", icon_size=14,
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
        self._tb_layout.insertWidget(1, self._interval_btn, 0, Qt.AlignVCenter)

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
            "background: transparent; color: #ffffff;"
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

    def input_ports(self) -> list[str]:
        return ["image"]

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._star_overlay.setGeometry(0, 0, self.width(), self.height())
        self._reposition_edge_handles()
        self._update_vec_display()

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
        color = theme.TEXT if self._display_on else theme.TEXT_DIM
        self._scan_btn.set_icon("calculator", color)

    def _update_star_icon(self) -> None:
        color = theme.TEXT if self._star_on else theme.TEXT_DIM
        self._star_btn.set_icon("star-dashed", color)

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
        if seconds is None:
            self._interval_btn.setText("off")
            self._timer.stop()
        else:
            label = f"{seconds:.2f}s".rstrip('0').rstrip('.')
            if label.endswith('.'):
                label += '0'
            self._interval_btn.setText(label)
            was_active = self._timer.isActive()
            self._timer.stop()
            self._timer.setInterval(int(seconds * 1000))
            if was_active:
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
            self._timer.start()

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
        self._embed_busy = False
        self._model_name = model_name
        self._last_vec   = vec
        self._vec_dim    = len(vec)
        self._set_status("ok")
        if self._star_on:
            self._star_overlay.set_vector(vec)

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

    Left side: triangular indent if has_input=True.
    Right side: triangular bump if has_output=True.
    Background is white; icon and label use the workspace gray so they
    read as 'cut out' of the white shape.
    """

    def __init__(
        self,
        label:      str,
        item_type:  str,
        icon_name:  str | None = None,
        has_input:  bool = False,
        has_output: bool = False,
        parent:     QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._item_type       = item_type
        self._has_input       = has_input
        self._has_output      = has_output
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

        icon_color = _WORKSPACE_BG  # dark gray — reads as cut-out on white
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
        """White core always spans x=0 … x=w-cw so all items share the same
        left and right edges.  Output connector bumps right to x=w.
        Input socket is painted separately as a dark overlay."""
        w, h = self.width(), self.height()
        r  = _MODULE_CORNER
        cw = _CONNECTOR_W
        ch = _CONNECTOR_H
        cy = h / 2
        rw = w - cw   # right boundary of the core rectangle

        path = QPainterPath()
        path.moveTo(r, 0)
        path.lineTo(rw - r, 0)
        path.quadTo(rw, 0, rw, r)

        if self._has_output:
            path.lineTo(rw, cy - ch / 2)
            path.lineTo(w,  cy)
            path.lineTo(rw, cy + ch / 2)

        path.lineTo(rw, h - r)
        path.quadTo(rw, h, rw - r, h)
        path.lineTo(r, h)
        path.quadTo(0, h, 0, h - r)
        path.lineTo(0, r)
        path.quadTo(0, 0, r, 0)
        path.closeSubpath()
        return path

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # White body — same core rect for every item
        painter.fillPath(self._build_path(), _MODULE_BG_COLOR)
        # Input socket: dark triangle eating into the left edge of the white pill
        if self._has_input:
            cw = _CONNECTOR_W
            ch = _CONNECTOR_H
            cy = self.height() / 2
            socket = QPainterPath()
            socket.moveTo(0, cy - ch / 2)
            socket.lineTo(cw, cy)
            socket.lineTo(0, cy + ch / 2)
            socket.closeSubpath()
            painter.fillPath(socket, _MODULE_SOCKET_COLOR)
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
            pix = _svg_icon(self._icon_name, 16, _WORKSPACE_BG).pixmap(16, 16)
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

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(_PANEL_W)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 8, 12)
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
                icon_name="video-camera",
                has_input=False,
                has_output=True,
                parent=items_widget,
            )
        )
        group_layout.addWidget(
            ModuleItem(
                label="Frame Vector",
                item_type="frame_vector",
                icon_name="calculator",
                has_input=True,
                has_output=False,
                parent=items_widget,
            )
        )

        outer.addWidget(items_widget)
        outer.addStretch()

    _PANEL_COLOR = QColor("#404040")

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self._PANEL_COLOR)
        p.end()


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
        _prefs.set(_PREFS_CONNS,   self._workspace.connections_state())

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

