#!/usr/bin/env python3
"""Shotlist Visualizer — frame-precise shot editing and annotation review.

Merges the former Shotlist Visualizer and Annotation Visualizer into one window:
  · Left column   : video player + timeline scrubber
  · Middle column : annotation panel (fields/json/txt/vector/mapping) — collapsible
  · Right column  : Scene table + Shot table (collapsible) + all buttons
"""

import sys
import os
import re
import subprocess
import json
import threading
import time
import random
import traceback
import faulthandler
from pathlib import Path

# Allow imports from the tool root (data/, services/, generators/)
sys.path.insert(0, str(Path(__file__).parent.parent))

from styles import theme

try:
    import av as _av
    import sounddevice as _sd
    _AUDIO_AVAILABLE = True
except ImportError:
    _AUDIO_AVAILABLE = False

# Fix Qt plugin conflict with OpenCV
# Import PyQt5 first, then remove OpenCV's Qt plugin path
from PyQt5.QtCore import Qt, QTimer, QEvent, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel,
    QMessageBox, QSizePolicy, QSlider, QStyle, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QFrame,
    QTextEdit, QGridLayout,
)
from styles.theme import GripSplitter
from PyQt5.QtGui import QFont, QPixmap, QImage, QColor, QMouseEvent
from PyQt5.QtCore import pyqtSignal as _pyqtSignal, QThread as _QThread

from data.shotlist import read_shotlist, write_shotlist, get_shotlist_path, attach_shot_ids
from data.metadata import get_metadata
from data.media_id import compute_media_id
from data.annotate import reindex_annotations_for_merge, reindex_annotations_for_split
from data.index import (
    load_mapping,
    serialize_annotation_item,
    get_embeddings_path,
    load_embeddings,
)


# ---------------------------------------------------------------------------
# IPC: socket server so external processes can tell a running Shotlist
# Visualizer to load a specific film without opening a second window.
# ---------------------------------------------------------------------------

def _ipc_socket_path(project_path: str) -> Path:
    """Return a per-project socket file path inside the system temp dir."""
    import tempfile, hashlib
    h = hashlib.md5(str(project_path).encode()).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"crossing_shotlist_{h}.sock"


class _IpcServer(_QThread):
    """Listens on a Unix-domain socket and emits load_requested(filename, media_type)."""

    load_requested = _pyqtSignal(str, str)   # filename, media_type

    def __init__(self, project_path: str, parent=None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self._running = True

    def run(self) -> None:
        import socket as _socket
        sock_path = _ipc_socket_path(self._project_path)
        # Remove stale socket file
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            srv.bind(str(sock_path))
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
                    if msg.get("action") == "load":
                        self.load_requested.emit(
                            msg.get("filename", ""),
                            msg.get("media_type", "movies"),
                        )
                except Exception:
                    pass
                finally:
                    conn.close()
        finally:
            srv.close()
            try:
                sock_path.unlink()
            except FileNotFoundError:
                pass

    def stop(self) -> None:
        self._running = False


def ipc_send_load(project_path: str, filename: str, media_type: str) -> bool:
    """Send a load request to a running Shotlist Visualizer.

    Returns True if the message was delivered, False if no server is listening.
    """
    import socket as _socket
    sock_path = _ipc_socket_path(project_path)
    if not sock_path.exists():
        return False
    try:
        conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        conn.settimeout(2.0)
        conn.connect(str(sock_path))
        msg = json.dumps({"action": "load", "filename": filename, "media_type": media_type})
        conn.sendall(msg.encode())
        conn.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


def _get_sar(video_path: str) -> tuple:
    """Return (sar_num, sar_den) for video_path via ffprobe. Falls back to (1,1)."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=sample_aspect_ratio",
                "-of", "json",
                video_path,
            ],
            capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout)
        sar_str = data["streams"][0].get("sample_aspect_ratio", "1:1")
        if sar_str in ("", "0:1", "1:1"):
            return (1, 1)
        parts = sar_str.replace("/", ":").split(":")
        return (int(parts[0]), int(parts[1]))
    except Exception:
        return (1, 1)

import cv2
import numpy as np
if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


def frames_to_timecode(frame_number: int, fps: float) -> str:
    """Convert a frame number to HH:MM:SS.mmm timecode string."""
    total_ms = int(round(frame_number / fps * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


class AudioPlayer:
    """Streams audio from a video file in a background thread (PyAV + sounddevice)."""

    def __init__(self, verbose: bool = False):
        self._stop_event = threading.Event()
        self._thread = None
        self._verbose = verbose

    def play(self, video_path: str, start_secs: float):
        """Start audio playback from start_secs. Stops any current playback first."""
        self.stop()
        if not _AUDIO_AVAILABLE:
            return
        if self._verbose:
            print(f"[audio] play  {Path(video_path).name}  @{start_secs:.2f}s", file=sys.stderr, flush=True)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._stream,
            args=(str(video_path), start_secs, self._stop_event, self._verbose),
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        """Signal audio to stop. Non-blocking — daemon thread finishes on its own."""
        self._stop_event.set()
        self._thread = None

    @staticmethod
    def _stream(video_path: str, start_secs: float, stop: threading.Event, verbose: bool = False):
        container = None
        # Map PyAV format names to numpy dtypes (mirrors av.audio.frame.format_dtypes).
        _fmt_dtype = {
            'dbl': 'f8', 'dblp': 'f8',
            'flt': 'f4', 'fltp': 'f4',
            's16': 'i2', 's16p': 'i2',
            's32': 'i4', 's32p': 'i4',
            'u8': 'u1',  'u8p': 'u1',
        }
        try:
            container = _av.open(video_path)
            audio_streams = [s for s in container.streams if s.type == 'audio']
            if not audio_streams:
                return
            audio_stream = audio_streams[0]
            sample_rate = audio_stream.codec_context.sample_rate
            channels = audio_stream.codec_context.channels or 2
            if verbose:
                codec_name = audio_stream.codec_context.codec.name if audio_stream.codec_context.codec else '?'
                print(
                    f"[audio] stream  {Path(video_path).name}"
                    f"  codec={codec_name}  {sample_rate}Hz  ch={channels}",
                    file=sys.stderr, flush=True,
                )
            if start_secs > 0:
                # seek uses microseconds in container time base
                container.seek(int(start_secs * 1_000_000))
            frame_count = 0
            with _sd.OutputStream(samplerate=sample_rate, channels=channels, dtype='float32') as out:
                for frame in container.decode(audio_stream):
                    if stop.is_set():
                        break
                    n = frame.samples
                    dtype = _fmt_dtype.get(frame.format.name, 'f4')
                    nc = frame.layout.nb_channels or channels
                    if verbose and frame_count == 0:
                        print(
                            f"[audio] first frame  fmt={frame.format.name}  samples={n}"
                            f"  channels={nc}  planes={len(frame.planes)}"
                            f"  linesize={frame.planes[0].buffer_size if frame.planes else '?'}",
                            file=sys.stderr, flush=True,
                        )
                    frame_count += 1
                    if frame.format.is_planar:
                        # bytes(p) copies each plane out of libav's buffer pool into a
                        # Python-owned bytes object before numpy touches the memory.
                        # count=n trims any alignment padding present in linesize[0].
                        # This prevents np.vstack from reading libav-managed memory while
                        # codec C threads may be recycling it (FF_THREAD_FRAME).
                        available = len(frame.planes)
                        use_planes = min(available, nc)
                        if verbose and available != nc:
                            print(
                                f"[audio] plane/channel mismatch  channels={nc} planes={available}"
                                f"  file={Path(video_path).name}",
                                file=sys.stderr, flush=True,
                            )
                        if use_planes <= 0:
                            continue
                        plane_arrays = []
                        for i in range(use_planes):
                            plane_arrays.append(np.frombuffer(bytes(frame.planes[i]), dtype=dtype, count=n))
                        # Keep stream channel count stable; if fewer planes are exposed,
                        # pad missing channels with silence.
                        while len(plane_arrays) < nc:
                            plane_arrays.append(np.zeros(n, dtype=dtype))
                        pcm = np.ascontiguousarray(np.column_stack(plane_arrays))
                    else:
                        raw = np.frombuffer(bytes(frame.planes[0]), dtype=dtype, count=n * nc)
                        pcm = raw.reshape(n, nc)
                    out.write(pcm)
        except Exception:
            pass
        finally:
            if verbose:
                print(f"[audio] stopped  {Path(video_path).name}  ({frame_count} frames decoded)", file=sys.stderr, flush=True)
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass


def _display_name(filename: str) -> str:
    """Return a clean display name from a filename (strips TMDb ID suffix)."""
    name = Path(filename).stem
    name = re.sub(r'\s*\{tmdb-\d+\}', '', name).strip()
    return name


class ClickSeekSlider(QSlider):
    """A QSlider that jumps to the exact position on a single click."""
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            opt = self.style().subControlRect(
                QStyle.CC_Slider, self._style_option(), QStyle.SC_SliderGroove, self
            )
            groove_width = opt.width()
            if groove_width > 0:
                ratio = (event.x() - opt.x()) / groove_width
                ratio = max(0.0, min(1.0, ratio))
                value = round(self.minimum() + ratio * (self.maximum() - self.minimum()))
                self.setValue(value)
        super().mousePressEvent(event)

    def _style_option(self):
        from PyQt5.QtWidgets import QStyleOptionSlider
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return opt


# ---------------------------------------------------------------------------
# Annotation helpers
# ---------------------------------------------------------------------------

def _get_annotation_json_path(project_path: str, filename: str, media_type: str) -> Path:
    stem = Path(filename).stem
    return Path(project_path) / "data" / "annotations" / "shots" / media_type / f"{stem}.json"


def _read_annotation_json(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _build_annotation_index(entries: list) -> dict:
    """Build a dict keyed by string shot_id → annotation dict."""
    idx = {}
    for entry in entries:
        shot = entry.get("shot")
        if not isinstance(shot, dict):
            continue
        shot_id = shot.get("shot_id")
        ann = shot.get("annotation")
        if shot_id is not None and ann is not None:
            idx[str(shot_id)] = ann
    return idx


def _build_entry_index(entries: list) -> dict:
    """Build a dict keyed by string shot_id → index in the entries list."""
    idx = {}
    for i, entry in enumerate(entries):
        shot = entry.get("shot")
        if not isinstance(shot, dict):
            continue
        shot_id = shot.get("shot_id")
        if shot_id is not None:
            idx[str(shot_id)] = i
    return idx


def _build_embedding_row_index(entries: list) -> dict:
    """Build a dict keyed by string shot_id → row index in the embeddings array."""
    idx = {}
    for row, entry in enumerate(entries):
        shot = entry.get("shot")
        if not isinstance(shot, dict):
            continue
        shot_id = shot.get("shot_id")
        if shot_id is not None:
            idx[str(shot_id)] = row
    return idx


def _is_valid_annotation(ann) -> bool:
    return isinstance(ann, dict) and "setting" in ann


# ---------------------------------------------------------------------------
# Shot table row builder  (5 cols: status | shot | start | stop | ignore)
# ---------------------------------------------------------------------------

def _make_shot_row(index: int, shot: dict, annotation, edited: bool, has_ann_file: bool) -> list:
    """Return [status, shot, start, stop, ignore] as QTableWidgetItems."""
    if not has_ann_file:
        status = ""
        color  = QColor(theme.TEXT)
    elif annotation is None:
        status = "?"
        color  = QColor(theme.TEXT_DIM)
    elif _is_valid_annotation(annotation):
        status = ("✎" if edited else "") + "✓"
        color  = QColor("#88ff88")
    else:
        status = ("✎" if edited else "") + "✗"
        color  = QColor("#ff8888")

    shot_str    = f"{index:04d}"
    start_str   = shot.get("start_time", f"f{shot.get('start_frame', '?')}")
    stop_str    = shot.get("end_time",   f"f{shot.get('end_frame',   '?')}")
    ignored_str = "✗" if shot.get("Ignore", "No") == "Yes" else ""

    items = [
        QTableWidgetItem(status),
        QTableWidgetItem(shot_str),
        QTableWidgetItem(start_str),
        QTableWidgetItem(stop_str),
        QTableWidgetItem(ignored_str),
    ]
    for item in items:
        item.setForeground(color)
        item.setTextAlignment(Qt.AlignCenter)
    return items


# ---------------------------------------------------------------------------
# Background annotation worker
# ---------------------------------------------------------------------------

class AnnotateWorker(QThread):
    shot_done = pyqtSignal(int)
    finished  = pyqtSignal(str)

    def __init__(self, project_path, filename, media_type, model_name, frames_per_shot,
                 shot_indices=None, shots=None):
        super().__init__()
        self._stop_event    = threading.Event()
        self.project_path   = project_path
        self.filename       = filename
        self.media_type     = media_type
        self.model_name     = model_name
        self.frames_per_shot = frames_per_shot
        self.shot_indices   = shot_indices
        self.shots          = shots

    def stop(self):
        self._stop_event.set()

    def run(self):
        from data.annotate import annotate_file_shots
        try:
            summary = annotate_file_shots(
                project_path=self.project_path,
                filename=self.filename,
                media_type=self.media_type,
                model_name=self.model_name,
                frames_per_shot=self.frames_per_shot,
                skip_existing=True,
                verbose=True,
                on_shot_done=lambda i: self.shot_done.emit(i),
                stop_event=self._stop_event,
            )
            updated = summary.get("updated", 0)
            skipped = summary.get("skipped", 0)
            self.finished.emit(f"✓ Done  {updated} annotated  {skipped} skipped")
        except Exception as exc:
            self.finished.emit(f"✗ {exc}")


# ---------------------------------------------------------------------------
# Merged visualizer
# ---------------------------------------------------------------------------

class ShotlistVisualizer(QMainWindow):
    """Frame-precise shot editing and annotation review — merged Shotlist + Annotation window."""

    def __init__(self, project_path: str, filenames: list, current_index: int = 0,
                 media_type: str = "movies", verbose: bool = False):
        super().__init__()
        self._verbose = verbose

        # ---- Core state ----
        self.project_path        = project_path
        self.media_type          = media_type

        # Expand the provided filenames list to include ALL films of this
        # media type known to the project, so the combo is always fully
        # populated regardless of how many files were passed as arguments.
        try:
            from data.metadata import get_metadata as _get_meta
            all_entries = _get_meta(project_path, media_type=media_type)
            all_filenames = [e["filename"] for e in all_entries if e.get("filename")]
        except Exception:
            all_filenames = []
        # Preserve order: all known files first, then any extras from the
        # caller that aren't in metadata (e.g. manually specified files).
        seen = set(all_filenames)
        for fn in filenames:
            if fn not in seen:
                all_filenames.append(fn)
                seen.add(fn)
        # Determine which index to start at (the first filename in the
        # original argument list, or 0 if nothing matches).
        start_filename = filenames[current_index] if filenames else None
        if start_filename and start_filename in all_filenames:
            resolved_index = all_filenames.index(start_filename)
        else:
            resolved_index = current_index

        self.filenames           = all_filenames if all_filenames else filenames
        self.current_movie_index = resolved_index
        self.filename            = self.filenames[resolved_index]
        self.shots: list         = []
        self.current_shot_index  = 0
        self.modified            = False

        # ---- Video state ----
        self.cap                  = None
        self.is_playing           = False
        self.current_frame_number = 0
        self.playback_timer       = None
        self._updating_slider     = False
        self._updating_combo      = False
        self._play_start_time     = 0.0
        self._play_start_frame    = 0
        self._current_shot_end_frame = 0
        self.audio                = AudioPlayer(verbose=self._verbose)

        # ---- Gremlins ----
        self.gremlins_active = False
        self.gremlins_timer  = QTimer()
        self.gremlins_timer.setInterval(5000)
        self.gremlins_timer.timeout.connect(self.gremlin_tick)

        # ---- Annotation state ----
        self.annotation_index: dict         = {}
        self._annotation_entry_index: dict  = {}
        self._embedding_row_index: dict     = {}
        self._embeddings                    = None
        self._embeddings_loaded: bool       = False
        self._mapping                       = None
        self._mapping_loaded                = False
        self._mapping_error: str            = ""
        self._annotate_worker               = None
        self._edited_shots: set             = set()
        self._ann_dirty                     = False
        self._updating_ann_display          = False
        self._has_ann_file: bool            = False

        try:
            import prefs as _prefs
            self._model_name      = _prefs.get("model_annotate", "gemma4-e4b")
            self._frames_per_shot = int(_prefs.get("annotate_frames_per_shot", 3))
        except Exception:
            self._model_name      = "gemma4-e4b"
            self._frames_per_shot = 3

        self._open_video()
        self._load_data()

        self.playback_timer = QTimer()
        self.playback_timer.timeout.connect(self._advance_frame_inner)
        interval = int(1000 / self.frame_rate) if self.frame_rate > 0 else 42
        self.playback_timer.setInterval(interval)

        self.setWindowTitle(
            f"Shotlist Visualizer \u2014 {_display_name(self.filename)}  "
            f"({resolved_index + 1}/{len(self.filenames)})"
        )

        self._init_ui()
        self.load_first_shot()

        # Start the IPC server so the Metadata Visualizer (and CLI) can
        # tell this window to load a different film without opening a second instance.
        self._ipc_server = _IpcServer(project_path, self)
        self._ipc_server.load_requested.connect(self._on_ipc_load)
        self._ipc_server.start()

    def _on_ipc_load(self, filename: str, media_type: str) -> None:
        """Handle a load request arriving from the IPC socket."""
        self.raise_()
        self.activateWindow()
        if media_type != self.media_type:
            # Different media type — not supported mid-session; ignore silently.
            return
        if filename in self.filenames:
            idx = self.filenames.index(filename)
            self.switch_to_movie(idx)
        else:
            # Film not in the current playlist — add it and switch.
            self.filenames.append(filename)
            self._updating_combo = True
            self.movie_combo.addItem(_display_name(filename), filename)
            self._updating_combo = False
            self.switch_to_movie(len(self.filenames) - 1)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _open_video(self):
        self.video_path = (
            Path(self.project_path) / "media" / "videos"
            / self.media_type / self.filename
        )
        if not self.video_path.exists():
            QMessageBox.critical(self, "Error", f"Video file not found:\n{self.video_path}")
            sys.exit(1)
        self.cap = cv2.VideoCapture(str(self.video_path))
        if not self.cap.isOpened():
            QMessageBox.critical(self, "Error", f"Could not open video:\n{self.video_path}")
            sys.exit(1)
        self.frame_rate          = self.cap.get(cv2.CAP_PROP_FPS)
        self.total_frames        = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        raw_w                    = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.video_native_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.sar_num, self.sar_den = _get_sar(str(self.video_path))
        self.video_native_width  = int(round(raw_w * self.sar_num / self.sar_den))

    def _load_data(self):
        """Load shotlist and annotations for the current film."""
        try:
            self.shots = read_shotlist(self.project_path, self.filename, self.media_type)
        except FileNotFoundError as e:
            QMessageBox.critical(self, "Error", str(e))
            sys.exit(1)
        for s in self.shots:
            for k in ("start_frame", "end_frame"):
                v = s.get(k)
                if isinstance(v, str):
                    try:
                        s[k] = int(v)
                    except ValueError:
                        s[k] = 0

        # Compute stable media_id and attach shot_ids to all shots
        _entries = get_metadata(self.project_path, media_type=self.media_type)
        _meta = next((e for e in _entries if e.get("filename") == self.filename), {})
        self.media_id = compute_media_id(_meta, self.media_type)
        attach_shot_ids(self.shots, self.media_id)

        ann_path    = _get_annotation_json_path(self.project_path, self.filename, self.media_type)
        ann_entries = _read_annotation_json(ann_path)
        self._has_ann_file              = ann_path.exists()
        self.annotation_index           = _build_annotation_index(ann_entries)
        self._annotation_entry_index    = _build_entry_index(ann_entries)
        self._embedding_row_index       = _build_embedding_row_index(ann_entries)
        self._embeddings                = None
        self._embeddings_loaded         = False

    def _reload_for_movie(self, index: int):
        """Reload video + data for movie *index*, then refresh the UI."""
        self.current_movie_index = index
        self.filename            = self.filenames[index]
        self._edited_shots.clear()
        self._ann_dirty = False
        if self.cap is not None:
            self.cap.release()
        self._open_video()
        self._load_data()

        interval = int(1000 / self.frame_rate) if self.frame_rate > 0 else 42
        self.playback_timer.setInterval(interval)

        self.setWindowTitle(
            f"Shotlist Visualizer \u2014 {_display_name(self.filename)}  "
            f"({index + 1}/{len(self.filenames)})"
        )

        self._updating_combo = True
        self.movie_combo.setCurrentIndex(index)
        self._updating_combo = False

        self.timeline_slider.setMaximum(max(0, self.total_frames - 1))
        self.timeline_slider.setValue(0)
        self.save_button.setEnabled(False)
        self.annotate_button.setChecked(False)
        self.annotate_button.setText("\u26a1 Auto-Annotate")

        self.rebuild_shot_list()
        self.rebuild_scene_list()
        self.update_stats()
        self.load_first_shot()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _init_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        h_splitter = GripSplitter(Qt.Horizontal)
        outer.addWidget(h_splitter, stretch=1)

        # ---- COL 1: video + slider ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(2)

        self.frame_label = QLabel()
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setScaledContents(False)
        self.frame_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.frame_label.setMinimumSize(1, 1)
        left_layout.addWidget(self.frame_label, stretch=1)

        self.timeline_slider = ClickSeekSlider(Qt.Horizontal)
        self.timeline_slider.setMinimum(0)
        self.timeline_slider.setMaximum(max(0, self.total_frames - 1))
        self.timeline_slider.setValue(0)
        self.timeline_slider.setFocusPolicy(Qt.NoFocus)
        self.timeline_slider.valueChanged.connect(self._on_timeline_seek)
        self.timeline_slider.setToolTip("Scrub timeline  [←/→ frame  Shift+←/→ 1s]")
        left_layout.addWidget(self.timeline_slider)

        # ---- COL 2: annotation panel (collapsible via splitter handle) ----
        mid = QWidget()
        mid_layout = QVBoxLayout(mid)
        mid_layout.setContentsMargins(2, 2, 2, 2)
        mid_layout.setSpacing(4)

        self.ann_repr_combo = QComboBox()
        self.ann_repr_combo.setFocusPolicy(Qt.NoFocus)
        for _mode in ("fields", "json", "txt", "vector", "mapping"):
            self.ann_repr_combo.addItem(_mode)
        self.ann_repr_combo.setCurrentIndex(0)
        self.ann_repr_combo.currentIndexChanged.connect(self._on_repr_changed)
        mid_layout.addWidget(self.ann_repr_combo)

        self.ann_display = QTextEdit()
        self.ann_display.setReadOnly(True)
        self.ann_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.ann_display.textChanged.connect(self._on_ann_text_changed)
        self.ann_display.installEventFilter(self)
        self.ann_display.hide()  # hidden when mode == "fields"
        mid_layout.addWidget(self.ann_display, stretch=1)

        self.ann_fields_table = QTableWidget()
        self.ann_fields_table.setColumnCount(1)
        self.ann_fields_table.horizontalHeader().hide()
        self.ann_fields_table.verticalHeader().hide()
        self.ann_fields_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.ann_fields_table.horizontalHeader().setStretchLastSection(True)
        self.ann_fields_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.ann_fields_table.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.SelectedClicked |
            QAbstractItemView.EditKeyPressed
        )
        self.ann_fields_table.setWordWrap(True)
        self.ann_fields_table.setFont(theme.font_mono())
        self.ann_fields_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.ann_fields_table.itemChanged.connect(self._on_fields_cell_changed)
        mid_layout.addWidget(self.ann_fields_table, stretch=1)

        self.ann_dirty_label = QLabel()
        self.ann_dirty_label.setFont(theme.font_mono())
        self.ann_dirty_label.setStyleSheet(f"color: {theme.ACCENT};")
        self.ann_dirty_label.hide()
        mid_layout.addWidget(self.ann_dirty_label)

        # ---- COL 3: shotlist + controls ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(2, 2, 2, 2)
        right_layout.setSpacing(4)

        # Movie selector (always visible — outside the vertical splitter)
        self.movie_combo = QComboBox()
        self.movie_combo.setFocusPolicy(Qt.NoFocus)
        for fn in self.filenames:
            self.movie_combo.addItem(_display_name(fn), fn)
        self.movie_combo.setCurrentIndex(self.current_movie_index)
        self.movie_combo.currentIndexChanged.connect(self.on_movie_combo_changed)
        right_layout.addWidget(self.movie_combo)

        # Vertical splitter: tables (top, collapsible) / controls (bottom)
        v_splitter = GripSplitter(Qt.Vertical)
        right_layout.addWidget(v_splitter, stretch=1)

        # -- Tables widget --
        tables_widget = QWidget()
        tables_layout = QHBoxLayout(tables_widget)
        tables_layout.setContentsMargins(0, 0, 0, 0)
        tables_layout.setSpacing(1)

        _tbl = theme.table_stylesheet()

        self.scene_list = QTableWidget()
        self.scene_list.setColumnCount(1)
        self.scene_list.setHorizontalHeaderLabels(["Scene"])
        self.scene_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.scene_list.verticalHeader().setVisible(False)
        self.scene_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.scene_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.scene_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.scene_list.setShowGrid(True)
        self.scene_list.setGridStyle(Qt.SolidLine)
        self.scene_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.scene_list.setMaximumWidth(68)
        self.scene_list.setMinimumWidth(56)
        self.scene_list.setFocusPolicy(Qt.NoFocus)
        self.scene_list.cellClicked.connect(self.on_scene_selected)
        self.scene_list.setToolTip("Scenes — click to jump  [PgUp/PgDn navigate  N/M split/merge]")
        self.scene_list.setFrameShape(QFrame.NoFrame)
        self.scene_list.setStyleSheet(_tbl)
        tables_layout.addWidget(self.scene_list)

        self.shot_list = QTableWidget()
        self.shot_list.setColumnCount(5)
        self.shot_list.setHorizontalHeaderLabels(["\u2713", "Shot", "Start", "Stop", "Ignore"])
        self.shot_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.shot_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.shot_list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.shot_list.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.shot_list.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.shot_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.shot_list.verticalHeader().setVisible(False)
        self.shot_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.shot_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.shot_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.shot_list.setShowGrid(True)
        self.shot_list.setGridStyle(Qt.SolidLine)
        self.shot_list.setFrameShape(QFrame.NoFrame)
        self.shot_list.installEventFilter(self)
        self.shot_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.shot_list.setToolTip("Shots \u2014 click to jump  (click Stop col to show end frame)  [\u2191/\u2193 navigate]")
        self.shot_list.cellClicked.connect(self.on_shot_selected)
        self.shot_list.setStyleSheet(_tbl)
        tables_layout.addWidget(self.shot_list, stretch=1)

        v_splitter.addWidget(tables_widget)

        # -- Controls widget --
        controls_widget = QWidget()
        controls_layout = QVBoxLayout(controls_widget)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(4)

        self.stats_label = QLabel()
        self.stats_label.setFont(theme.font_mono())
        self.stats_label.setWordWrap(True)
        self.stats_label.setStyleSheet(f"background-color: {theme.INPUT_BG}; padding: 4px;")
        controls_layout.addWidget(self.stats_label)

        self.info_label = QLabel()
        self.info_label.setFont(theme.font_mono())
        self.info_label.setWordWrap(True)
        self.info_label.setMinimumHeight(60)
        self.info_label.setStyleSheet(f"background-color: {theme.INPUT_BG}; padding: 4px;")
        controls_layout.addWidget(self.info_label)

        # Button grid — 4 rows x 3 cols
        btn_grid = QGridLayout()
        btn_grid.setSpacing(4)

        # Row 0: annotation actions
        self.annotate_button = QPushButton("\u26a1 Auto-Annotate")
        self.annotate_button.setCheckable(True)
        self.annotate_button.setChecked(False)
        self.annotate_button.clicked.connect(self._toggle_auto_annotate)
        self.annotate_button.setFocusPolicy(Qt.NoFocus)
        self.annotate_button.setToolTip(
            "Start / stop background LLM annotation of unannotated shots in this film"
        )
        btn_grid.addWidget(self.annotate_button, 0, 0)

        self.remove_ann_button = QPushButton("\U0001f5d1 Remove")
        self.remove_ann_button.clicked.connect(self._remove_current_annotation)
        self.remove_ann_button.setFocusPolicy(Qt.NoFocus)
        self.remove_ann_button.setToolTip("Delete the annotation for the currently selected shot")
        btn_grid.addWidget(self.remove_ann_button, 0, 1)

        self.remove_all_ann_button = QPushButton("\U0001f5d1 Remove All")
        self.remove_all_ann_button.clicked.connect(self._remove_all_annotations)
        self.remove_all_ann_button.setFocusPolicy(Qt.NoFocus)
        self.remove_all_ann_button.setToolTip("Delete all shot annotations for this film (cannot be undone)")
        btn_grid.addWidget(self.remove_all_ann_button, 0, 2)

        # Row 1: shot editing
        self.split_button = QPushButton("New Shot")
        self.split_button.clicked.connect(self.split_shot_at_current_frame)
        self.split_button.setFocusPolicy(Qt.NoFocus)
        self.split_button.setToolTip("Split current shot at current frame  [Shift+N]")
        btn_grid.addWidget(self.split_button, 1, 0)

        self.merge_button = QPushButton("Merge Shot")
        self.merge_button.clicked.connect(self.merge_with_previous)
        self.merge_button.setFocusPolicy(Qt.NoFocus)
        self.merge_button.setToolTip("Merge current shot with previous shot  [Shift+M]")
        btn_grid.addWidget(self.merge_button, 1, 1)

        self.ignore_button = QPushButton("\u2297 Ignore")
        self.ignore_button.clicked.connect(self.toggle_current_ignore)
        self.ignore_button.setFocusPolicy(Qt.NoFocus)
        self.ignore_button.setToolTip("Toggle Ignore on current shot  [I]")
        btn_grid.addWidget(self.ignore_button, 1, 2)

        # Row 2: scene editing
        self.split_scene_button = QPushButton("New Scene")
        self.split_scene_button.clicked.connect(self.split_scene_at_current_shot)
        self.split_scene_button.setFocusPolicy(Qt.NoFocus)
        self.split_scene_button.setToolTip("Start a new scene at current shot  [N]")
        btn_grid.addWidget(self.split_scene_button, 2, 0)

        self.merge_scene_button = QPushButton("Merge Scene")
        self.merge_scene_button.clicked.connect(self.merge_scene_at_current_shot)
        self.merge_scene_button.setFocusPolicy(Qt.NoFocus)
        self.merge_scene_button.setToolTip("Merge current scene into previous scene  [M]")
        btn_grid.addWidget(self.merge_scene_button, 2, 1)

        self.save_button = QPushButton("\U0001f4be Save")
        self.save_button.clicked.connect(self.save_changes)
        self.save_button.setEnabled(False)
        self.save_button.setFocusPolicy(Qt.NoFocus)
        self.save_button.setToolTip("Save shotlist changes to CSV  [Ctrl+S]")
        btn_grid.addWidget(self.save_button, 2, 2)

        # Row 3: playback
        self.play_pause_button = QPushButton("\u25b6 Play")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.play_pause_button.setFocusPolicy(Qt.NoFocus)
        self.play_pause_button.setToolTip("Play / Pause  [Space]")
        btn_grid.addWidget(self.play_pause_button, 3, 0)

        self.continue_button = QPushButton("Continue")
        self.continue_button.setCheckable(True)
        self.continue_button.setChecked(True)
        self.continue_button.setFocusPolicy(Qt.NoFocus)
        self.continue_button.setToolTip("When OFF: playback stops at the end of the current shot  [C]")
        btn_grid.addWidget(self.continue_button, 3, 1)

        self.gremlins_button = QPushButton("\U0001f47e Gremlins")
        self.gremlins_button.setCheckable(True)
        self.gremlins_button.setChecked(False)
        self.gremlins_button.clicked.connect(self.toggle_gremlins)
        self.gremlins_button.setFocusPolicy(Qt.NoFocus)
        self.gremlins_button.setToolTip("Randomly jump movies/timecodes every 5 s  [G]")
        btn_grid.addWidget(self.gremlins_button, 3, 2)

        # Row 3 annotation buttons moved to row 0

        controls_layout.addLayout(btn_grid)

        hint = QLabel("\u2191\u2193 shot  PgUp/Dn scene  Space play  \u2190\u2192 frame  Shift+\u2190\u2192 1s  Home/End movie")
        hint.setFont(theme.font_mono())
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        controls_layout.addWidget(hint)

        v_splitter.addWidget(controls_widget)
        v_splitter.setStretchFactor(0, 1)
        v_splitter.setStretchFactor(1, 0)

        # ---- Assemble horizontal splitter ----
        h_splitter.addWidget(left)
        h_splitter.addWidget(mid)
        h_splitter.addWidget(right)
        mid.setMinimumWidth(220)
        right.setMinimumWidth(200)
        h_splitter.setStretchFactor(0, 3)
        h_splitter.setStretchFactor(1, 1)
        h_splitter.setStretchFactor(2, 0)
        self._h_splitter   = h_splitter
        self._right_widget = right

        self.rebuild_shot_list()
        self.rebuild_scene_list()
        self.update_stats()

        QTimer.singleShot(0, self._fit_right_panel)
        self.setFocus()

    def _fit_right_panel(self):
        """Auto-size the right splitter panel to the natural width of the shot table."""
        hdr = self.shot_list.horizontalHeader()
        for c in range(self.shot_list.columnCount()):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.shot_list.resizeColumnsToContents()
        table_w = sum(self.shot_list.columnWidth(c) for c in range(self.shot_list.columnCount()))
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        table_w += self.shot_list.verticalScrollBar().sizeHint().width()
        margins = self._right_widget.layout().contentsMargins()
        right_w = table_w + margins.left() + margins.right() + self.scene_list.maximumWidth() + 4
        sizes   = self._h_splitter.sizes()
        total   = sum(sizes)
        mid_w   = sizes[1]
        left_w  = max(200, total - mid_w - right_w)
        self._h_splitter.setSizes([left_w, mid_w, right_w])

    def _update_timeline_slider(self):
        """Sync the timeline slider to the current frame without triggering a seek."""
        self._updating_slider = True
        self.timeline_slider.setValue(self.current_frame_number)
        self._updating_slider = False

    def _on_timeline_seek(self, value: int):
        """Handle timeline slider movement — seek to that frame."""
        if self._updating_slider:
            return
        if self.is_playing:
            self.stop_playback()
        self.current_frame_number = value
        self.update_current_shot_from_frame()
        frame = self._get_frame(self.current_frame_number)
        if frame is not None:
            self._display_frame(frame)
        self.update_frame_info()

    def _get_frame(self, frame_number: int) -> np.ndarray | None:
        """Extract a specific frame using OpenCV (frame-precise)."""
        if frame_number < 0 or frame_number >= self.total_frames:
            return None
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        ret, frame = self.cap.read()
        return frame if ret else None

    def _display_frame(self, frame: np.ndarray):
        """Display an OpenCV frame in the Qt label."""
        if frame is None:
            self.frame_label.setText("Failed to load frame")
            return
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        if (self.sar_num, self.sar_den) != (1, 1):
            display_w = int(round(frame_rgb.shape[1] * self.sar_num / self.sar_den))
            frame_rgb = cv2.resize(frame_rgb, (display_w, frame_rgb.shape[0]),
                                   interpolation=cv2.INTER_LINEAR)
        height, width, _ = frame_rgb.shape
        q_image = QImage(frame_rgb.data, width, height, 3 * width, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(q_image)
        if not pixmap.isNull():
            # Scale to fit window while maintaining aspect ratio
            scaled_pixmap = pixmap.scaled(
                self.frame_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )
            self.frame_label.setPixmap(scaled_pixmap)
    
    def toggle_play_pause(self):
        """Toggle video playback."""
        if self.is_playing:
            self.stop_playback()
        else:
            self.start_playback()
    
    def start_playback(self):
        """Start video playback from current position."""
        self.is_playing = True
        self.play_pause_button.setText("⏸ Pause")
        self._play_start_frame = self.current_frame_number
        start_secs = self.current_frame_number / self.frame_rate if self.frame_rate > 0 else 0.0
        self.audio.play(str(self.video_path), start_secs)
        # Delay video timer start to let audio initialize, then anchor the clock
        QTimer.singleShot(130, self._begin_video_timer)

    def _begin_video_timer(self):
        """Called after audio startup delay — anchors the wall clock and starts the video timer."""
        try:
            if not self.is_playing:
                return
            self._play_start_time = time.perf_counter()
            self.playback_timer.start()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.stop_playback()
    
    def stop_playback(self):
        """Stop video playback."""
        self.is_playing = False
        self.play_pause_button.setText("▶ Play")
        self.playback_timer.stop()
        self.audio.stop()
    
    def toggle_gremlins(self):
        """Toggle gremlins mode on/off."""
        self.gremlins_active = not self.gremlins_active
        self.gremlins_button.setChecked(self.gremlins_active)
        if self.gremlins_active:
            self.gremlin_tick()
            self.gremlins_timer.start()
        else:
            self.gremlins_timer.stop()

    def gremlin_tick(self):
        """Jump to a random movie and a random shot index, using the same path as manual navigation."""
        try:
            self._gremlin_tick_inner()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.gremlins_active = False
            self.gremlins_timer.stop()
            self.gremlins_button.setChecked(False)

    def _gremlin_tick_inner(self):
        if not self.gremlins_active:
            return
        was_playing = self.is_playing
        index = random.randrange(len(self.filenames))
        if self._verbose:
            print(
                f"[gremlins] → [{index}] {self.filenames[index]}"
                f"  (was [{self.current_movie_index}] {self.filenames[self.current_movie_index]})",
                file=sys.stderr, flush=True,
            )
        if index != self.current_movie_index:
            if self.is_playing:
                self.stop_playback()
            self._reload_for_movie(index)
        if self.shots:
            shot_index = random.randrange(len(self.shots))
            if self._verbose:
                shot = self.shots[shot_index]
                print(f"[gremlins] shot {shot_index}/{len(self.shots)}  frame={shot.get('start_frame', '?')}", file=sys.stderr, flush=True)
            self.is_playing = was_playing
            self.jump_to_shot(shot_index)
    
    def advance_frame(self):
        """Advance to next frame during playback, driven by wall-clock time to stay in sync with audio."""
        try:
            self._advance_frame_inner()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.stop_playback()

    def _advance_frame_inner(self):
        if not self.is_playing:
            return

        elapsed      = time.perf_counter() - self._play_start_time
        target_frame = self._play_start_frame + int(elapsed * self.frame_rate)
        target_frame = min(target_frame, self.total_frames - 1)

        if target_frame <= self.current_frame_number:
            return

        self.current_frame_number = target_frame

        if self.current_frame_number >= self.total_frames:
            self.stop_playback()
            return

        # Stop at shot boundary when Continue is off
        if not self.continue_button.isChecked():
            if 0 <= self.current_shot_index < len(self.shots):
                end_frame = int(self.shots[self.current_shot_index].get('end_frame', self.total_frames - 1))
                if self.current_frame_number > end_frame:
                    self.current_frame_number = end_frame
                    frame = self._get_frame(end_frame)
                    if frame is not None:
                        self._display_frame(frame)
                    self._update_timeline_slider()
                    self.update_frame_info()
                    self.stop_playback()
                    return

        self.update_current_shot_from_frame()
        self._update_timeline_slider()
        frame = self._get_frame(self.current_frame_number)
        if frame is not None:
            self._display_frame(frame)
        self.update_frame_info()
    def update_current_shot_from_frame(self):
        """Update current shot index based on current frame number."""
        for i, shot in enumerate(self.shots):
            start_frame = int(shot.get('start_frame', 0))
            end_frame   = int(shot.get('end_frame',   0))
            if start_frame <= self.current_frame_number <= end_frame:
                if i != self.current_shot_index:
                    self.current_shot_index = i
                    self.shot_list.selectRow(i)
                    self.sync_scene_list_selection()
                    self._update_annotation_panel(i, shot)
                return
    
    def _append_shot_row(self, index: int, shot: dict):
        """Insert one row at the end of the shot table."""
        shot_id  = shot.get("shot_id", "")
        ann      = self.annotation_index.get(shot_id)
        edited   = index in self._edited_shots
        has_file = self._has_ann_file
        row      = self.shot_list.rowCount()
        self.shot_list.insertRow(row)
        for col, item in enumerate(_make_shot_row(index, shot, ann, edited, has_file)):
            self.shot_list.setItem(row, col, item)

    def _refresh_shot_row(self, index: int):
        """Re-render a single row without rebuilding the whole table."""
        shot     = self.shots[index]
        shot_id  = shot.get("shot_id", "")
        ann      = self.annotation_index.get(shot_id)
        edited   = index in self._edited_shots
        has_file = self._has_ann_file
        for col, item in enumerate(_make_shot_row(index, shot, ann, edited, has_file)):
            old = self.shot_list.item(index, col)
            if old:
                old.setText(item.text())
                old.setForeground(item.foreground())

    def rebuild_shot_list(self):
        """Rebuild the shot table from self.shots."""
        self.shot_list.setRowCount(0)
        for i, shot in enumerate(self.shots):
            self._append_shot_row(i, shot)

    def rebuild_scene_list(self):
        """Rebuild the scene table from unique Scene values in self.shots."""
        self.scene_list.setRowCount(0)
        seen = set()
        for shot in self.shots:
            scene = int(shot.get('Scene', 0))
            if scene not in seen:
                seen.add(scene)
                row = self.scene_list.rowCount()
                self.scene_list.insertRow(row)
                item = QTableWidgetItem(str(scene))
                item.setData(Qt.UserRole, scene)
                item.setTextAlignment(Qt.AlignCenter)
                self.scene_list.setItem(row, 0, item)

    def on_movie_combo_changed(self, index: int):
        """Handle movie selection from the dropdown."""
        if self._updating_combo:
            return
        self.switch_to_movie(index)

    def switch_to_movie(self, index: int):
        """Switch to a different movie in the playlist, prompting to save first if needed."""
        if index == self.current_movie_index:
            return

        if self.modified:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Save before switching?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
            )
            if reply == QMessageBox.Cancel:
                self._updating_combo = True
                self.movie_combo.setCurrentIndex(self.current_movie_index)
                self._updating_combo = False
                return
            elif reply == QMessageBox.Save:
                self.save_changes()

        if self._annotate_worker is not None:
            self._annotate_worker.requestInterruption()

        was_playing = self.is_playing
        if self.is_playing:
            self.stop_playback()

        self._reload_for_movie(index)

        if was_playing:
            self.start_playback()

    def sync_scene_list_selection(self):
        """Highlight the scene_list row matching the current shot's scene."""
        if not (0 <= self.current_shot_index < len(self.shots)):
            return
        current_scene = int(self.shots[self.current_shot_index].get('Scene', 0))
        for i in range(self.scene_list.rowCount()):
            if self.scene_list.item(i, 0).data(Qt.UserRole) == current_scene:
                self.scene_list.selectRow(i)
                return

    def on_scene_selected(self, row: int, col: int = 0):
        """Jump to the first shot of the selected scene."""
        item = self.scene_list.item(row, 0)
        if item is None:
            return
        scene = item.data(Qt.UserRole)
        for i, shot in enumerate(self.shots):
            if int(shot.get('Scene', 0)) == scene:
                self.jump_to_shot(i)
                return

    def prev_scene(self):
        """Jump to the first shot of the previous scene."""
        if not (0 <= self.current_shot_index < len(self.shots)):
            return
        current_scene = int(self.shots[self.current_shot_index].get('Scene', 0))
        # Walk backwards to find the first shot of the previous scene
        for i in range(self.current_shot_index - 1, -1, -1):
            if int(self.shots[i].get('Scene', 0)) < current_scene:
                target_scene = int(self.shots[i].get('Scene', 0))
                # Find the first shot in that scene
                for j, shot in enumerate(self.shots):
                    if int(shot.get('Scene', 0)) == target_scene:
                        self.jump_to_shot(j)
                        return
                return

    def next_scene(self):
        """Jump to the first shot of the next scene."""
        if not (0 <= self.current_shot_index < len(self.shots)):
            return
        current_scene = int(self.shots[self.current_shot_index].get('Scene', 0))
        for i in range(self.current_shot_index + 1, len(self.shots)):
            if int(self.shots[i].get('Scene', 0)) > current_scene:
                self.jump_to_shot(i)
                return

    def load_first_shot(self):
        """Load the first shot."""
        if self.shots:
            self.jump_to_shot(0)
    
    def jump_to_shot(self, index: int, show_end: bool = False):
        """Jump to a specific shot and display its first or last frame."""
        was_playing = self.is_playing
        if self.is_playing:
            self.stop_playback()

        if not (0 <= index < len(self.shots)):
            return

        self._discard_ann_edit()
        self.current_shot_index = index
        shot = self.shots[index]

        frame_number = int(shot.get('end_frame' if show_end else 'start_frame', 0))
        self._current_shot_end_frame = int(shot.get('end_frame', frame_number))

        self.current_frame_number = frame_number
        self._update_timeline_slider()

        frame = self._get_frame(frame_number)
        if frame is not None:
            self._display_frame(frame)
        self.update_frame_info()

        self.shot_list.selectRow(index)
        self.sync_scene_list_selection()
        self.update_buttons()
        self._update_annotation_panel(index, shot)

        if was_playing and not show_end:
            self.start_playback()
    
    def show_end_frame(self):
        """Show the end frame of the current shot."""
        self.jump_to_shot(self.current_shot_index, show_end=True)
    
    def next_shot(self):
        """Move to next shot."""
        if self.current_shot_index < len(self.shots) - 1:
            self.jump_to_shot(self.current_shot_index + 1)
    
    def prev_shot(self):
        """Move to previous shot."""
        if self.current_shot_index > 0:
            self.jump_to_shot(self.current_shot_index - 1)
    
    def next_frame(self):
        """Step forward one frame."""
        if self.is_playing:
            self.stop_playback()
        nxt = self.current_frame_number + 1
        if nxt < self.total_frames:
            self.current_frame_number = nxt
            self.update_current_shot_from_frame()
            self._update_timeline_slider()
            frame = self._get_frame(self.current_frame_number)
            if frame is not None:
                self._display_frame(frame)
            self.update_frame_info()

    def prev_frame(self):
        """Step backward one frame."""
        if self.is_playing:
            self.stop_playback()
        prv = self.current_frame_number - 1
        if prv >= 0:
            self.current_frame_number = prv
            self.update_current_shot_from_frame()
            self._update_timeline_slider()
            frame = self._get_frame(self.current_frame_number)
            if frame is not None:
                self._display_frame(frame)
            self.update_frame_info()

    def step_seconds(self, direction: int):
        """Step forward or backward by approximately one second."""
        if self.is_playing:
            self.stop_playback()
        frames = max(1, int(round(self.frame_rate)))
        target = max(0, min(self.total_frames - 1, self.current_frame_number + frames * direction))
        self.current_frame_number = target
        self.update_current_shot_from_frame()
        self._update_timeline_slider()
        frame = self._get_frame(self.current_frame_number)
        if frame is not None:
            self._display_frame(frame)
        self.update_frame_info()
    
    def update_frame_info(self):
        """Update info label with current frame details."""
        if not (0 <= self.current_shot_index < len(self.shots)):
            return
        shot       = self.shots[self.current_shot_index]
        scene      = shot.get('Scene', '0')
        start_tc   = shot.get('start_time', '?')
        end_tc     = shot.get('end_time', '?')
        confidence = shot.get('Shot_Confidence', '')
        conf_str   = f"\nConf: {confidence}" if confidence else ""
        shot_id    = shot.get('shot_id', '')
        sid_str    = f"\n{shot_id}" if shot_id else ""
        self.info_label.setText(
            f"Scene {scene}  Shot #{self.current_shot_index}\n"
            f"Frame: {self.current_frame_number}\n"
            f"{start_tc} → {end_tc}{conf_str}{sid_str}"
        )
    
    def on_shot_selected(self, row: int, col: int = 0):
        """Handle shot selection from table. Col 3 (Stop) shows the end frame."""
        self.jump_to_shot(row, show_end=(col == 3))
    
    def toggle_current_ignore(self):
        """Toggle ignore status for current shot."""
        if 0 <= self.current_shot_index < len(self.shots):
            shot = self.shots[self.current_shot_index]
            shot['Ignore'] = 'No' if shot.get('Ignore', 'No') == 'Yes' else 'Yes'
            self._refresh_shot_row(self.current_shot_index)
            self.modified = True
            self.save_button.setEnabled(True)
            self.update_stats()
    
    def merge_with_previous(self):
        """Merge current shot with previous shot."""
        if self.current_shot_index <= 0:
            QMessageBox.warning(self, "Cannot Merge", "Cannot merge first shot.")
            return
        
        # Get current and previous shots
        current_shot = self.shots[self.current_shot_index]
        prev_shot = self.shots[self.current_shot_index - 1]

        # Capture stable shot_ids BEFORE modification (both become invalid after merge)
        from data.media_id import build_shot_id as _bsid
        ids_to_remove = {
            current_shot.get("shot_id", ""),
            prev_shot.get("shot_id", ""),
        }
        ids_to_remove.discard("")
        
        # Update previous shot's end to current shot's end
        prev_shot['end_time'] = current_shot['end_time']
        prev_shot['end_frame'] = current_shot['end_frame']
        # Assign new stable shot_id to the merged shot
        prev_shot['shot_id'] = _bsid(
            self.media_id,
            int(prev_shot.get('start_frame') or 0),
            int(prev_shot.get('end_frame') or 0),
        )
        
        # Remove current shot
        self.shots.pop(self.current_shot_index)

        # Remove invalidated annotations from disk and memory
        reindex_annotations_for_merge(
            self.project_path, self.filename, self.media_type,
            ids_to_remove,
        )
        for sid in ids_to_remove:
            self.annotation_index.pop(sid, None)
            self._annotation_entry_index.pop(sid, None)
        self._edited_shots.discard(self.current_shot_index)
        self._edited_shots.discard(self.current_shot_index - 1)

        # Rebuild lists
        self.rebuild_shot_list()
        self.rebuild_scene_list()

        # Jump to merged shot
        self.current_shot_index -= 1
        self.jump_to_shot(self.current_shot_index)
        
        self.modified = True
        self.save_button.setEnabled(True)
        self.update_stats()
    
    def split_shot_at_current_frame(self):
        """Split the current shot at the current frame, creating a new shot boundary."""
        if not (0 <= self.current_shot_index < len(self.shots)):
            return

        shot = self.shots[self.current_shot_index]
        start_frame = int(shot.get('start_frame', 0))
        end_frame = int(shot.get('end_frame', 0))
        split_frame = self.current_frame_number

        # Split frame must be strictly inside the shot (not at start or end)
        if split_frame <= start_frame:
            QMessageBox.warning(self, "Cannot Split",
                "Cannot split at the start frame — move forward at least one frame.")
            return
        if split_frame > end_frame:
            QMessageBox.warning(self, "Cannot Split",
                "Current frame is beyond the end of this shot.")
            return

        # Capture stable shot_id BEFORE modifying the shot
        original_shot_id = shot.get("shot_id", "")
        from data.media_id import build_shot_id as _bsid

        # Build the two new shots
        first_shot = dict(shot)
        first_shot['end_frame'] = split_frame - 1
        first_shot['end_time'] = frames_to_timecode(split_frame - 1, self.frame_rate)
        first_shot['Shot_Caption'] = ''
        first_shot['Shot_Confidence'] = ''
        first_shot['shot_id'] = _bsid(
            self.media_id,
            int(first_shot.get('start_frame') or 0),
            int(first_shot['end_frame']),
        )

        second_shot = dict(shot)
        second_shot['start_frame'] = split_frame
        second_shot['start_time'] = frames_to_timecode(split_frame, self.frame_rate)
        second_shot['Shot_Caption'] = ''
        second_shot['Shot_Source'] = 'manual'
        second_shot['Shot_Confidence'] = ''
        second_shot['shot_id'] = _bsid(
            self.media_id,
            int(second_shot['start_frame']),
            int(second_shot.get('end_frame') or 0),
        )

        # Replace the current shot with the two new ones
        self.shots[self.current_shot_index:self.current_shot_index + 1] = [first_shot, second_shot]

        # Delete the original shot's annotation from disk and memory
        ids_to_remove = {original_shot_id} if original_shot_id else set()
        reindex_annotations_for_split(
            self.project_path, self.filename, self.media_type,
            ids_to_remove,
        )
        for sid in ids_to_remove:
            self.annotation_index.pop(sid, None)
            self._annotation_entry_index.pop(sid, None)
        self._edited_shots.discard(self.current_shot_index)

        # Rebuild lists
        self.rebuild_shot_list()
        self.rebuild_scene_list()

        # Land on the new (second) shot
        self.jump_to_shot(self.current_shot_index + 1)

        self.modified = True
        self.save_button.setEnabled(True)
        self.update_stats()

    def split_scene_at_current_shot(self):
        """Start a new scene at the current shot: increment Scene for current and all subsequent shots."""
        if not (0 <= self.current_shot_index < len(self.shots)):
            return
        for i in range(self.current_shot_index, len(self.shots)):
            self.shots[i]['Scene'] = str(int(self.shots[i].get('Scene', 0)) + 1)
        self.rebuild_shot_list()
        self.rebuild_scene_list()
        self.shot_list.selectRow(self.current_shot_index)
        self.sync_scene_list_selection()
        self.modified = True
        self.save_button.setEnabled(True)
        self.update_stats()

    def merge_scene_at_current_shot(self):
        """Merge current shot's scene into the previous: decrement Scene for current and all subsequent shots."""
        if not (0 <= self.current_shot_index < len(self.shots)):
            return
        current_scene = int(self.shots[self.current_shot_index].get('Scene', 0))
        if current_scene <= 0:
            QMessageBox.warning(self, "Cannot Merge Scene",
                "Current shot is already in scene 0.")
            return
        for i in range(self.current_shot_index, len(self.shots)):
            self.shots[i]['Scene'] = str(max(0, int(self.shots[i].get('Scene', 0)) - 1))
        self.rebuild_shot_list()
        self.rebuild_scene_list()
        self.shot_list.selectRow(self.current_shot_index)
        self.sync_scene_list_selection()
        self.modified = True
        self.save_button.setEnabled(True)
        self.update_stats()

    def update_stats(self):
        """Update statistics display."""
        total_shots   = len(self.shots)
        ignored_shots = sum(1 for shot in self.shots if shot.get('Ignore', 'No') == 'Yes')
        active_shots  = total_shots - ignored_shots
        total_scenes  = len(set(shot.get('Scene', '0') for shot in self.shots))
        ann_count     = len(self.annotation_index)
        ann_str       = f"  Annotated: {ann_count}/{total_shots}" if self._has_ann_file else ""
        self.stats_label.setText(
            f"Scenes: {total_scenes}  Shots: {total_shots}\n"
            f"Active: {active_shots}  Ignored: {ignored_shots}{ann_str}"
        )
    
    def update_buttons(self):
        """Update button states based on current position."""
        self.merge_button.setEnabled(self.current_shot_index > 0)
    
    def save_changes(self):
        """Save modified shotlist."""
        try:
            write_shotlist(self.project_path, self.filename, self.media_type, self.shots)
            self.modified = False
            self.save_button.setEnabled(False)
            QMessageBox.information(self, "Saved", "Shotlist saved successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save shotlist:\n{e}")
    
    def eventFilter(self, obj, event):
        """Intercept events from child widgets to handle keyboard shortcuts globally."""
        try:
            if event.type() == QEvent.KeyPress:
                key  = event.key()
                mods = event.modifiers()
                if obj == self.ann_display:
                    # Ctrl+S saves a dirty JSON edit; Escape discards it.
                    # QTextEdit consumes these before they reach keyPressEvent, so
                    # we must intercept them here.
                    if key == Qt.Key_S and mods & Qt.ControlModifier:
                        if self._ann_dirty:
                            self._save_annotation_edit()
                        return True
                    if key == Qt.Key_Escape:
                        if self._ann_dirty:
                            self._discard_ann_edit()
                        return True
                elif obj == self.shot_list:
                    # Redirect keyboard events to main window
                    if key in (Qt.Key_Space, Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down,
                               Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End,
                               Qt.Key_C, Qt.Key_E, Qt.Key_F, Qt.Key_I, Qt.Key_M, Qt.Key_N, Qt.Key_G):
                        self.keyPressEvent(event)
                        return True
            return super().eventFilter(obj, event)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return super().eventFilter(obj, event)
    
    def keyPressEvent(self, event):
        """Handle keyboard shortcuts."""
        key  = event.key()
        mods = event.modifiers()
        if key in (Qt.Key_Q, Qt.Key_W) and mods & Qt.ControlModifier:
            self.close()
            return
        if key == Qt.Key_Space:
            self.toggle_play_pause()
        elif key == Qt.Key_Left:
            if mods & Qt.ShiftModifier:
                self.step_seconds(-1)
            else:
                self.prev_frame()
        elif key == Qt.Key_Right:
            if mods & Qt.ShiftModifier:
                self.step_seconds(1)
            else:
                self.next_frame()
        elif key == Qt.Key_Up:
            self.prev_shot()
        elif key == Qt.Key_Down:
            self.next_shot()
        elif key == Qt.Key_PageUp:
            self.prev_scene()
        elif key == Qt.Key_PageDown:
            self.next_scene()
        elif key == Qt.Key_E:
            self.show_end_frame()
        elif key == Qt.Key_C:
            self.continue_button.setChecked(not self.continue_button.isChecked())
        elif key == Qt.Key_I:
            self.toggle_current_ignore()
        elif key == Qt.Key_M:
            if mods & Qt.ShiftModifier:
                self.merge_with_previous()
            else:
                self.merge_scene_at_current_shot()
        elif key == Qt.Key_N:
            if mods & Qt.ShiftModifier:
                self.split_shot_at_current_frame()
            else:
                self.split_scene_at_current_shot()
        elif key == Qt.Key_S and mods & Qt.ControlModifier:
            if self._ann_dirty:
                self._save_annotation_edit()
            elif self.modified:
                self.save_changes()
        elif key == Qt.Key_G:
            self.toggle_gremlins()
        elif key == Qt.Key_Home:
            if self.current_movie_index > 0:
                self.switch_to_movie(self.current_movie_index - 1)
        elif key == Qt.Key_End:
            if self.current_movie_index < len(self.filenames) - 1:
                self.switch_to_movie(self.current_movie_index + 1)
        else:
            super().keyPressEvent(event)
    
    def closeEvent(self, event):
        """Handle window close."""
        if self._annotate_worker is not None:
            self._annotate_worker.requestInterruption()

        self.gremlins_timer.stop()
        if self.is_playing:
            self.stop_playback()

        if self.cap is not None:
            self.cap.release()

        # Stop the IPC server thread cleanly.
        if hasattr(self, "_ipc_server"):
            self._ipc_server.stop()
            self._ipc_server.wait(1000)

        if self.modified:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
            )
            if reply == QMessageBox.Save:
                self.save_changes()
                event.accept()
            elif reply == QMessageBox.Discard:
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


    # ------------------------------------------------------------------ #
    #  Annotation panel                                                    #
    # ------------------------------------------------------------------ #

    def _update_annotation_panel(self, index: int, shot: dict):
        """Refresh the annotation display panel for a given shot."""
        shot_id = shot.get("shot_id", "") if isinstance(shot, dict) else ""
        ann = self.annotation_index.get(shot_id)
        mode = self.ann_repr_combo.currentText()
        if mode == "fields":
            self.ann_display.hide()
            self.ann_fields_table.show()
            self._populate_fields_table(ann, shot)
        else:
            self.ann_fields_table.hide()
            self.ann_display.show()
            self.ann_display.blockSignals(True)
            if mode == "json":
                self.ann_display.setPlainText(self._render_annotation_json(ann))
            elif mode == "txt":
                self.ann_display.setPlainText(self._render_annotation_txt(ann))
            elif mode == "vector":
                self.ann_display.setPlainText(self._render_annotation_vector(index))
            elif mode == "mapping":
                self.ann_display.setPlainText(self._render_annotation_mapping(index))
            self.ann_display.setReadOnly(mode != "json")
            self.ann_display.blockSignals(False)
        self._ann_dirty = False
        self.ann_dirty_label.hide()

    def _populate_fields_table(self, ann: dict | None, shot: dict):
        """Fill ann_fields_table with alternating title/content rows."""
        tbl = self.ann_fields_table
        tbl.blockSignals(True)
        tbl.clearContents()
        if ann is None:
            tbl.setRowCount(1)
            item = QTableWidgetItem("(no annotation)")
            item.setFlags(Qt.ItemIsEnabled)
            item.setForeground(QColor(theme.TEXT_DIM))
            tbl.setItem(0, 0, item)
            tbl.blockSignals(False)
            return

        try:
            from data.index import load_fields
            ordered_keys = load_fields(self.project_path)
        except Exception:
            ordered_keys = [k for k in ann if k != "shot_index"]
        keys = [k for k in ordered_keys if k in ann]

        tbl.setRowCount(len(keys) * 2)
        title_bg   = QColor(theme.PANEL_BG)
        content_bg = QColor(theme.BG)
        text_fg    = QColor(theme.TEXT)

        for i, k in enumerate(keys):
            title_row   = i * 2
            content_row = i * 2 + 1

            # ---- title row (non-editable, bold) ----
            title_item = QTableWidgetItem(k)
            title_item.setFont(theme.font_mono(bold=True))
            title_item.setBackground(title_bg)
            title_item.setForeground(text_fg)
            title_item.setFlags(Qt.ItemIsEnabled)
            tbl.setItem(title_row, 0, title_item)

            # ---- content row (editable) ----
            v = ann[k]
            v_str = ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
            content_item = QTableWidgetItem(v_str)
            content_item.setBackground(content_bg)
            content_item.setForeground(text_fg)
            content_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable | Qt.ItemIsSelectable)
            tbl.setItem(content_row, 0, content_item)

        tbl.resizeRowsToContents()
        tbl.blockSignals(False)

    def _on_fields_cell_changed(self, item: "QTableWidgetItem"):
        """Called when a content cell in the fields table is edited."""
        row = item.row()
        if row % 2 == 0:
            # Title row — skip
            return

        key_item = self.ann_fields_table.item(row - 1, 0)
        if key_item is None:
            return
        key = key_item.text()

        raw = item.text()
        # Commas → list; otherwise plain string
        value = [v.strip() for v in raw.split(",")] if "," in raw else raw

        idx = self.current_shot_index
        shot_id = self.shots[idx].get("shot_id", "") if 0 <= idx < len(self.shots) else ""
        ann = self.annotation_index.get(shot_id)
        if ann is None:
            return
        ann[key] = value
        self.annotation_index[shot_id] = ann

        import json as _json
        path = _get_annotation_json_path(self.project_path, self.filename, self.media_type)
        if path is None:
            return
        try:
            if path.exists():
                with open(path, encoding="utf-8") as fh:
                    existing = _json.load(fh)
            else:
                existing = []
            entry_idx = self._annotation_entry_index.get(shot_id)
            if entry_idx is not None and 0 <= entry_idx < len(existing):
                existing[entry_idx]["shot"]["annotation"].update(ann)
            else:
                existing.append(ann)
                self._annotation_entry_index[shot_id] = len(existing) - 1
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_json.dumps(existing, indent=2, ensure_ascii=False))
            self._edited_shots.add(idx)
            self._refresh_shot_row(idx)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

        # Resize the edited row in case text wrapped
        self.ann_fields_table.resizeRowToContents(row)

    def _render_annotation_json(self, ann: dict | None) -> str:
        if ann is None:
            return "{}"
        import json as _json
        return _json.dumps(ann, indent=2, ensure_ascii=False)

    def _render_annotation_txt(self, ann: dict | None) -> str:
        if ann is None:
            return "(no annotation)"
        try:
            from data.index import load_mapping, serialize_annotation_item
            mapping = load_mapping(self.project_path)
            item = {"shot": {"annotation": ann}}
            return serialize_annotation_item(item, mapping)
        except FileNotFoundError:
            return "(no mapping file — run: crossing index)"
        except Exception as exc:
            return f"(error: {exc})"

    def _render_annotation_vector(self, index: int) -> str:
        emb = self._load_embeddings_cached()
        if emb is None:
            return "(no embeddings)"
        shot_id = self.shots[index].get("shot_id", "") if 0 <= index < len(self.shots) else ""
        row_idx = self._embedding_row_index.get(shot_id)
        if row_idx is None:
            return "(no embedding for this shot)"
        vec = emb[row_idx]
        return f"dim={len(vec)}\n[{', '.join(f'{x:.4f}' for x in vec[:8])} ...]"

    def _render_annotation_mapping(self, index: int) -> str:
        from pathlib import Path
        mapping_path = Path(self.project_path) / "preferences" / "data" / "mapping.yaml"
        if not mapping_path.exists():
            return f"(mapping file not found:\n{mapping_path})"
        try:
            return mapping_path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"(error reading mapping file: {exc})"

    def _get_mapping(self) -> dict:
        emb = self._load_embeddings_cached()
        if emb is None or len(emb) == 0:
            return {}
        if not hasattr(self, "_cached_mapping") or self._cached_mapping is None:
            try:
                import numpy as _np
                norms  = _np.linalg.norm(emb, axis=1, keepdims=True)
                normed = emb / _np.where(norms > 0, norms, 1)
                sim    = normed @ normed.T
                mapping: dict[int, dict[int, float]] = {}
                for row_idx, shot_idx in self._embedding_row_index.items():
                    sims = {
                        s2: float(sim[r2, shot_idx])
                        for r2, s2 in self._embedding_row_index.items()
                        if r2 != shot_idx
                    }
                    mapping[row_idx] = sims
                self._cached_mapping = mapping
            except Exception:
                self._cached_mapping = {}
        return self._cached_mapping

    def _load_embeddings_cached(self):
        if self._embeddings is not None:
            return self._embeddings
        emb_path = get_embeddings_path(self.project_path, self.filename, self.media_type)
        if emb_path.exists():
            try:
                self._embeddings = load_embeddings(emb_path)
            except Exception:
                self._embeddings = None
        return self._embeddings

    def _on_repr_changed(self):
        """Refresh annotation display when the user switches representation mode."""
        if 0 <= self.current_shot_index < len(self.shots):
            self._update_annotation_panel(
                self.current_shot_index, self.shots[self.current_shot_index]
            )

    # ------------------------------------------------------------------ #
    #  Annotation editing                                                  #
    # ------------------------------------------------------------------ #

    def _on_ann_text_changed(self):
        """Called whenever the annotation text display changes."""
        if not self._ann_dirty:
            mode = self.ann_repr_combo.currentText()
            if mode == "json":
                self._ann_dirty = True
                self.ann_dirty_label.setText("\u2022 unsaved annotation edit — Ctrl+S to save, Esc to discard")
                self.ann_dirty_label.show()

    def _discard_ann_edit(self):
        """Discard any pending annotation edit without saving."""
        if self._ann_dirty:
            self._ann_dirty = False
            self.ann_dirty_label.hide()
            self.ann_display.setReadOnly(True)

    def _save_annotation_edit(self):
        """Save the edited annotation JSON back to disk."""
        import json as _json
        try:
            text = self.ann_display.toPlainText().strip()
            data = _json.loads(text)
        except Exception as exc:
            QMessageBox.warning(self, "Invalid JSON", str(exc))
            return

        path = _get_annotation_json_path(self.project_path, self.filename, self.media_type)
        if path is None:
            QMessageBox.warning(self, "No annotation file", "Cannot determine annotation file path.")
            return

        try:
            if path.exists():
                with open(path, encoding="utf-8") as fh:
                    existing = _json.load(fh)
            else:
                existing = []
            idx = self.current_shot_index
            shot_id = self.shots[idx].get("shot_id", "") if 0 <= idx < len(self.shots) else ""
            entry_idx = self._annotation_entry_index.get(shot_id)
            if entry_idx is not None and 0 <= entry_idx < len(existing):
                existing[entry_idx]["shot"]["annotation"].update(data)
            else:
                data["shot_index"] = idx
                existing.append(data)
                entry_idx = len(existing) - 1
                self._annotation_entry_index[shot_id] = entry_idx

            # Serialise with json_repair-safe format
            out_text = _json.dumps(existing, indent=2, ensure_ascii=False)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(out_text)

            self.annotation_index[shot_id] = data
            self._edited_shots.add(idx)
            self._refresh_shot_row(idx)
            self._ann_dirty = False
            self.ann_dirty_label.hide()
            self.ann_display.setReadOnly(True)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    # ------------------------------------------------------------------ #
    #  Auto-annotation                                                     #
    # ------------------------------------------------------------------ #

    def _toggle_auto_annotate(self):
        """Start or stop background LLM annotation."""
        if self._annotate_worker is not None and self._annotate_worker.isRunning():
            self._annotate_worker.stop()
            self.annotate_button.setChecked(False)
            self.annotate_button.setText("\u26a1 Auto-Annotate")
            return

        if self.modified:
            QMessageBox.warning(
                self,
                "Unsaved Changes",
                "The shotlist has unsaved changes.\nSave before running Auto-Annotate.",
            )
            self.annotate_button.setChecked(False)
            return

        # Build list of un-annotated shot indices
        unannotated = [
            i for i, s in enumerate(self.shots)
            if s.get("shot_id", "") not in self.annotation_index
        ]
        if not unannotated:
            self.annotate_button.setChecked(False)
            QMessageBox.information(self, "Done", "All shots already annotated.")
            return

        self._annotate_worker = AnnotateWorker(
            project_path=self.project_path,
            filename=self.filename,
            media_type=self.media_type,
            shot_indices=unannotated,
            shots=self.shots,
            frames_per_shot=self._frames_per_shot,
            model_name=self._model_name,
        )
        self._annotate_worker.shot_done.connect(self._on_shot_annotated)
        self._annotate_worker.finished.connect(self._on_annotate_finished)
        self._annotate_worker.start()
        self.annotate_button.setChecked(True)
        self.annotate_button.setText("\u26a1 Stop Annotating")

    def _on_shot_annotated(self, index: int):
        """Called when a single shot has been annotated by the background worker."""
        shot_id = self.shots[index].get("shot_id", "") if 0 <= index < len(self.shots) else ""
        ann = self.annotation_index.get(shot_id)
        if ann is None:
            # Re-read from disk
            try:
                path = _get_annotation_json_path(self.project_path, self.filename, self.media_type)
                if path:
                    all_anns = _read_annotation_json(path)
                    self.annotation_index           = _build_annotation_index(all_anns)
                    self._annotation_entry_index    = _build_entry_index(all_anns)
                    self._embedding_row_index       = _build_embedding_row_index(all_anns)
            except Exception:
                pass
        self._refresh_shot_row(index)
        if index == self.current_shot_index:
            self._update_annotation_panel(index, self.shots[index])
        self.update_stats()

    def _on_annotate_finished(self, message: str):
        """Called when auto-annotation finishes (or is interrupted)."""
        self.annotate_button.setChecked(False)
        self.annotate_button.setText("\u26a1 Auto-Annotate")
        if self._annotate_worker is not None:
            self._annotate_worker.deleteLater()
            self._annotate_worker = None
        print(message, flush=True)

        # Reload annotation indexes from disk so partial runs are reflected.
        try:
            path = _get_annotation_json_path(self.project_path, self.filename, self.media_type)
            if path and path.exists():
                all_anns = _read_annotation_json(path)
                self.annotation_index        = _build_annotation_index(all_anns)
                self._annotation_entry_index = _build_entry_index(all_anns)
                self._embedding_row_index    = _build_embedding_row_index(all_anns)
                self._has_ann_file           = True
        except Exception as exc:
            print(f"Warning: could not reload annotations: {exc}", flush=True)

        self.rebuild_shot_list()
        self.update_stats()
        self._update_annotation_panel(
            self.current_shot_index,
            self.shots[self.current_shot_index] if self.shots else {},
        )

    def _remove_current_annotation(self):
        """Delete the annotation for the currently selected shot."""
        import json as _json
        idx = self.current_shot_index
        shot_id = self.shots[idx].get("shot_id", "") if 0 <= idx < len(self.shots) else ""
        path = _get_annotation_json_path(self.project_path, self.filename, self.media_type)
        if not path.exists() or shot_id not in self.annotation_index:
            QMessageBox.information(self, "No Annotation", "No annotation found for the selected shot.")
            return
        try:
            with open(path, encoding="utf-8") as fh:
                entries = _json.load(fh)
            # Remove the entry matching the stable shot_id
            entries = [
                e for e in entries
                if not (isinstance(e.get("shot"), dict)
                        and str(e["shot"].get("shot_id", "")) == shot_id)
            ]
            with open(path, "w", encoding="utf-8") as fh:
                _json.dump(entries, fh, indent=2, ensure_ascii=False)
            self.annotation_index.pop(shot_id, None)
            self._annotation_entry_index.pop(shot_id, None)
            self._embedding_row_index.pop(shot_id, None)
            self._has_ann_file = path.exists()
            self._refresh_shot_row(idx)
            self.update_stats()
            self._update_annotation_panel(idx, self.shots[idx] if self.shots else {})
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))

    def _remove_all_annotations(self):
        """Delete all annotation data for the current film."""
        path = _get_annotation_json_path(self.project_path, self.filename, self.media_type)
        if path is None or not path.exists():
            QMessageBox.information(self, "No Annotations", "No annotation file found.")
            return
        reply = QMessageBox.question(
            self, "Delete Annotations",
            f"Delete all annotations for {_display_name(self.filename)}?\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        try:
            path.unlink()
            self.annotation_index       = {}
            self._annotation_entry_index = {}
            self._embedding_row_index   = {}
            self._embeddings            = None
            self._has_ann_file          = False
            self._edited_shots.clear()
            self.rebuild_shot_list()
            self.update_stats()
            self._update_annotation_panel(self.current_shot_index,
                                          self.shots[self.current_shot_index] if self.shots else {})
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Validate shot boundaries with frame-precise display")
    parser.add_argument('query', nargs='?', help="Filename substring to match")
    parser.add_argument('--tmdb', type=int, help="TMDb ID")
    parser.add_argument('--media', choices=['movies', 'gameplay'], default='movies')
    parser.add_argument('--project', help="Project path (default: current directory)")
    parser.add_argument('--filenames', nargs='+', help="Explicit list of filenames (passed by cli.py)")
    parser.add_argument('--all', action='store_true', help="Validate all movies with shotlists")
    parser.add_argument('--verbose', action='store_true', help="Print audio/gremlins diagnostics to stderr (also writes a crash log)")

    args = parser.parse_args()

    # Determine project path
    if args.project:
        project_path = args.project
    else:
        cwd = Path.cwd()
        if (cwd / "data").exists():
            project_path = str(cwd)
        elif (cwd.parent / "data").exists():
            project_path = str(cwd.parent)
        else:
            print("✗ Error: Could not find project path. Use --project or run from project directory.", file=sys.stderr)
            sys.exit(1)

    # Resolve the list of filenames
    if args.filenames:
        filenames = args.filenames
    elif getattr(args, 'all', False):
        entries = get_metadata(project_path, media_type=args.media)
        filenames = [
            e['filename'] for e in entries
            if e.get('filename') and get_shotlist_path(project_path, e['filename'], args.media).exists()
        ]
        if not filenames:
            print("✗ Error: No shotlists found.", file=sys.stderr)
            sys.exit(1)
    elif args.tmdb:
        entries = get_metadata(project_path, media_type=args.media)
        filenames = [e['filename'] for e in entries if e.get('tmdb') == str(args.tmdb)]
        if not filenames:
            print(f"✗ Error: No file found with TMDb ID: {args.tmdb}", file=sys.stderr)
            sys.exit(1)
    elif args.query:
        entries = get_metadata(project_path, query=args.query, media_type=args.media)
        if not entries:
            print(f"✗ Error: No file found matching '{args.query}'", file=sys.stderr)
            sys.exit(1)
        elif len(entries) > 1:
            print(f"✗ Error: Multiple files match '{args.query}':", file=sys.stderr)
            for entry in entries:
                print(f"  - {entry['filename']}", file=sys.stderr)
            print("\nUse --tmdb <id> or a more specific query", file=sys.stderr)
            sys.exit(1)
        filenames = [entries[0]['filename']]
    else:
        print("✗ Error: Must provide query, --tmdb, --filenames, or --all", file=sys.stderr)
        sys.exit(1)

    # Verify shotlists exist
    for fn in filenames:
        shotlist_path = get_shotlist_path(project_path, fn, args.media)
        if not shotlist_path.exists():
            print(f"✗ Error: No shotlist found for {fn}", file=sys.stderr)
            print("Run 'crossing shotlist shot detect' first to generate shotlist.", file=sys.stderr)
            sys.exit(1)

    # Enable low-level fault handler so C-level crashes (segfaults etc.) print a traceback.
    # In verbose mode also write the crash output to a dedicated log file so it survives
    # even if the terminal scrollback is lost.
    _fault_log = None
    if args.verbose:
        _fault_log_path = Path(project_path) / "crossing_crash.log"
        _fault_log = open(_fault_log_path, 'w', buffering=1)  # line-buffered
        faulthandler.enable(file=_fault_log)
        print(f"[verbose] crash log → {_fault_log_path}", file=sys.stderr, flush=True)
        print(f"[verbose] launching with {len(filenames)} file(s): {filenames}", file=sys.stderr, flush=True)
    else:
        faulthandler.enable()

    # Launch Qt application
    app = QApplication(sys.argv)
    theme.apply_theme(app)
    visualizer = ShotlistVisualizer(project_path, filenames, 0, args.media, verbose=args.verbose)

    # Open maximised on whatever screen the window appears on
    screen = QApplication.primaryScreen()
    avail = screen.availableGeometry()
    visualizer.setGeometry(avail)
    visualizer.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()


# Backward-compat alias — legacy code that imports OpenCVShotVisualizer still works
OpenCVShotVisualizer = ShotlistVisualizer
