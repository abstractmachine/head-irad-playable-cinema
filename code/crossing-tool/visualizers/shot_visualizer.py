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
import math
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
    QMessageBox, QSizePolicy, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QFrame,
    QTextEdit, QGridLayout, QStackedLayout, QScrollArea, QTabWidget,
)
from styles.theme import GripSplitter, JumpScrollBar, save_window_geometry, restore_window_geometry
from PyQt5.QtGui import QFont, QPixmap, QImage, QColor, QPalette, QBrush
from PyQt5.QtCore import pyqtSignal as _pyqtSignal, QThread as _QThread
from tool.shortcuts import (
    KEY_PREV_TITLE, KEY_NEXT_TITLE,
    KEY_PREV_ITEM, KEY_NEXT_ITEM,
    KEY_PREV_SHOT, KEY_NEXT_SHOT,
    KEY_PREV_FRAME, KEY_NEXT_FRAME,
    KEY_PLAY_PAUSE,
)
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.metadata_block import MetadataBlock

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
    """Listens on a Unix-domain socket and emits load_requested(filename, media_type, shot_id, playback)."""

    load_requested = _pyqtSignal(str, str, str, str)   # filename, media_type, shot_id, playback

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
                            msg.get("media_type", "movie"),
                            msg.get("shot_id", ""),
                            msg.get("playback", "pause"),
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


def ipc_send_load(
    project_path: str,
    filename: str,
    media_type: str,
    shot_id: str = "",
    playback: str = "pause",
) -> bool:
    """Send a load request to a running Shotlist Visualizer.

    Parameters
    ----------
    shot_id:  Optional shot_id to jump to after loading the film.
    playback: ``"pause"`` (default) keeps the player stopped on the shot;
              ``"play"`` resumes playback after jumping.

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
        msg = json.dumps({
            "action":     "load",
            "filename":   filename,
            "media_type": media_type,
            "shot_id":    shot_id,
            "playback":   playback,
        })
        conn.sendall(msg.encode())
        conn.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


def open_at_shot(
    project_path: str,
    filename: str,
    media_type: str = "movie",
    shot_id: str = "",
    play: bool = False,
    loop: bool = False,
    no_continue: bool = False,
) -> None:
    """Open (or navigate) the Shotlist Visualizer to *filename* / *shot_id*.

    Delivers via IPC to a running instance first; if none is listening,
    spawns a new Shotlist Visualizer process directly.  This is the single
    shared entry-point used by all visualizers and CLI commands that want to
    open a film at a specific shot.
    """
    import subprocess as _sp
    if ipc_send_load(project_path, filename, media_type, shot_id=shot_id, playback="play" if play else "pause"):
        return
    cmd = [
        sys.executable, str(Path(__file__)),
        "--project",   project_path,
        "--media",     media_type,
        "--filenames", filename,
    ]
    if shot_id:
        cmd += ["--shot-id", shot_id]
    if play:
        cmd += ["--play"]
    if loop:
        cmd += ["--loop"]
    if no_continue:
        cmd += ["--no-continue"]
    _sp.Popen(cmd)


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
os.environ.setdefault("OPENCV_FFMPEG_READ_ATTEMPTS", "65536")
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

    def play(self, video_path: str, start_secs: float, gain_db: float = 0.0, channel_map: dict | None = None, on_start_callback=None):
        """Start audio playback from start_secs. Stops any current playback first."""
        self.stop()
        if not _AUDIO_AVAILABLE:
            return
        if self._verbose:
            print(
                f"[audio] play  {Path(video_path).name}  @{start_secs:.2f}s"
                f"  gain={gain_db:+.3f} dB  map={channel_map or {}}",
                file=sys.stderr,
                flush=True,
            )
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._stream,
            args=(str(video_path), start_secs, self._stop_event, self._verbose, float(gain_db), channel_map, on_start_callback),
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        """Signal audio to stop. Non-blocking — daemon thread finishes on its own."""
        self._stop_event.set()
        self._thread = None

    @staticmethod
    def _to_float32_pcm(pcm: np.ndarray) -> np.ndarray:
        """Convert decoded PCM to float32 in [-1, 1] for sounddevice output."""
        arr = np.asarray(pcm)
        if arr.dtype.kind == 'f':
            return arr.astype(np.float32, copy=False)

        if arr.dtype.kind == 'u':
            max_val = float(np.iinfo(arr.dtype).max)
            mid = max_val / 2.0
            out = (arr.astype(np.float32) - mid) / max(mid, 1.0)
            return out

        if arr.dtype.kind == 'i':
            info = np.iinfo(arr.dtype)
            scale = float(max(abs(info.min), info.max))
            out = arr.astype(np.float32) / max(scale, 1.0)
            return out

        return arr.astype(np.float32)

    @staticmethod
    def _channel_indices_from_map(channel_map: dict | None, available_channels: int) -> list[int]:
        """Resolve output channel indices from metadata mapping and stream width."""
        if available_channels <= 1:
            return [0]

        if isinstance(channel_map, dict):
            if "mono" in channel_map:
                try:
                    mono_idx = int(channel_map.get("mono", 0))
                except (TypeError, ValueError):
                    mono_idx = 0
                mono_idx = max(0, min(available_channels - 1, mono_idx))
                return [mono_idx]

            if "left" in channel_map and "right" in channel_map:
                try:
                    left = int(channel_map.get("left"))
                    right = int(channel_map.get("right"))
                except (TypeError, ValueError):
                    left, right = 0, 1
                left = max(0, min(available_channels - 1, left))
                right = max(0, min(available_channels - 1, right))
                if left == right:
                    return [left]
                return [left, right]

        return [0, 1]

    @staticmethod
    def _stream(
        video_path: str,
        start_secs: float,
        stop: threading.Event,
        verbose: bool = False,
        gain_db: float = 0.0,
        channel_map: dict | None = None,
        on_start_callback=None,
    ):
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
            mismatch_count = 0
            mismatch_logged = False
            gain_linear = math.pow(10.0, gain_db / 20.0)
            selected_channels = AudioPlayer._channel_indices_from_map(channel_map, channels)
            out_channels = max(1, len(selected_channels))
            with _sd.OutputStream(samplerate=sample_rate, channels=out_channels, dtype='float32') as out:
                first_frame = True
                for frame in container.decode(audio_stream):
                    if stop.is_set():
                        break
                    # Discard pre-seek frames (decoder starts from previous keyframe)
                    if start_secs > 0 and frame.time is not None and frame.time < start_secs:
                        continue
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
                        if available != nc:
                            mismatch_count += 1
                            if verbose and not mismatch_logged:
                                mismatch_logged = True
                                print(
                                    f"[audio] plane/channel mismatch  channels={nc} planes={available}"
                                    f"  file={Path(video_path).name}"
                                    "  (additional mismatches suppressed)",
                                    file=sys.stderr,
                                    flush=True,
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

                    pcm_f32 = AudioPlayer._to_float32_pcm(pcm)
                    if pcm_f32.ndim == 1:
                        pcm_f32 = pcm_f32.reshape(-1, 1)

                    frame_channels = pcm_f32.shape[1]
                    channel_arrays = []
                    for idx in selected_channels:
                        if 0 <= idx < frame_channels:
                            channel_arrays.append(pcm_f32[:, idx])
                        else:
                            channel_arrays.append(np.zeros(pcm_f32.shape[0], dtype=np.float32))
                    pcm_f32 = np.column_stack(channel_arrays)

                    if gain_linear != 1.0:
                        pcm_f32 = np.clip(pcm_f32 * gain_linear, -1.0, 1.0)
                    out.write(pcm_f32)
                    if first_frame:
                        latency = out.latency if hasattr(out, "latency") and out.latency else 0.0
                        audio_start_time = time.perf_counter() + latency
                        if on_start_callback:
                            on_start_callback.emit(audio_start_time, start_secs)
                        if verbose:
                            print(f"[sync] latency={latency:.4f}s", file=sys.stderr, flush=True)
                            print(f"[sync] audio_start={audio_start_time:.6f} start_secs={start_secs}", file=sys.stderr, flush=True)
                        first_frame = False
        except Exception:
            pass
        finally:
            if verbose:
                if mismatch_count > 0:
                    print(
                        f"[audio] plane/channel mismatch summary  {Path(video_path).name}"
                        f"  count={mismatch_count}",
                        file=sys.stderr,
                        flush=True,
                    )
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


# ---------------------------------------------------------------------------
# Annotation helpers
# ---------------------------------------------------------------------------

try:
    from services.frame_match import LOW_CONFIDENCE_THRESHOLD
except ImportError:
    LOW_CONFIDENCE_THRESHOLD = 0.18

# Best-frame column index
_BEST_COLUMN_INDEX = 3


def _allow_metadata_wrap(block: MetadataBlock) -> None:
    """Let a MetadataBlock's value labels wrap across lines instead of
    overflowing/clipping when the inspector is narrower than their content.

    Customizes the label instances directly via the block's public
    ``labels()`` accessor, so the shared MetadataBlock component's default
    fixed single-line row contract (used as-is by other visualizers) is left
    untouched.
    """
    for lbl in block.labels().values():
        lbl.setWordWrap(True)
        lbl.setMinimumHeight(0)
        lbl.setMaximumHeight(16777215)

def _get_annotation_json_path(project_path: str, filename: str, media_type: str) -> Path:
    stem = Path(filename).stem
    return Path(project_path) / "data" / "annotations" / "shots" / media_type / f"{stem}.annotations.json"


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


def _build_best_frame_index(entries: list) -> dict:
    """Build a dict keyed by string shot_id → best_frame dict."""
    idx = {}
    for entry in entries:
        shot = entry.get("shot")
        if not isinstance(shot, dict):
            continue
        shot_id = shot.get("shot_id")
        bf = shot.get("best_frame")
        if shot_id is not None and bf is not None:
            idx[str(shot_id)] = bf
    return idx


def _is_valid_annotation(ann) -> bool:
    return isinstance(ann, dict) and "setting" in ann


# ---------------------------------------------------------------------------
# Shot table row builder  (6 cols: status | shot | start | best | stop | ignore)
# ---------------------------------------------------------------------------

def _make_shot_row(index: int, shot: dict, annotation, edited: bool, has_ann_file: bool) -> list:
    """Return [status, shot, start, best, stop, ignore] as QTableWidgetItems."""
    if not has_ann_file:
        status = ""
        color  = QColor(theme.TEXT)
    elif annotation is None:
        status = "?"
        color  = QColor(theme.TEXT_DIM)
    elif _is_valid_annotation(annotation):
        status = ("✎" if edited else "") + "✓"
        color  = QColor(theme.TEXT)
    else:
        status = ("✎" if edited else "") + "✗"
        color  = QColor(theme.TEXT)

    shot_str    = f"{index:04d}"
    start_str   = shot.get("start_time", f"f{shot.get('start_frame', '?')}")
    stop_str    = shot.get("end_time",   f"f{shot.get('end_frame',   '?')}")
    ignored_str = "✗" if shot.get("Ignore", "No") == "Yes" else ""

    # Best-frame cell
    bf = shot.get("best_frame")
    if bf is None:
        best_text = ""
        best_bg   = None
        best_fg   = None
    else:
        frame  = bf.get("frame", 0)
        source = bf.get("source", "model")
        best_text = f"f{frame}"
        if source == "user":
            best_bg = QColor("#00aa00")
            best_fg = QColor("#ffffff")
        elif source == "fallback" or bf.get("score", 1.0) < LOW_CONFIDENCE_THRESHOLD:
            best_bg = QColor("#cc6600")
            best_fg = QColor("#ffffff")
        else:
            best_bg = None
            best_fg = QColor("#ffffff")  # always white text on Best cell when populated

    _DEFAULT_BG = QColor(theme.CELL_BG)
    items = [
        QTableWidgetItem(status),
        QTableWidgetItem(shot_str),
        QTableWidgetItem(start_str),
        QTableWidgetItem(best_text),
        QTableWidgetItem(stop_str),
        QTableWidgetItem(ignored_str),
    ]
    _white = QColor(theme.TEXT)
    for i, item in enumerate(items):
        item.setBackground(_DEFAULT_BG)
        # Status colour (green/red/dim) only on the status cell (col 0)
        item.setForeground(color if i == 0 else _white)
        item.setTextAlignment(Qt.AlignCenter)

    # Apply best-frame background/foreground to the Best column item specifically
    best_item = items[_BEST_COLUMN_INDEX]
    if best_bg is not None:
        best_item.setBackground(best_bg)
    if best_fg is not None:
        best_item.setForeground(best_fg)

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

    _audio_start_signal = pyqtSignal(float, float)

    def __init__(self, project_path: str, filenames: list, current_index: int = 0,
                 media_type: str = "movie", verbose: bool = False):
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

        # ---- Window mode state (Tab / Shift+Tab shortcuts) ----
        self._inspectors_hidden    = False
        self._saved_inspector_sizes = None

        # ---- Video state ----
        self.cap                  = None
        self.is_playing           = False
        self.current_frame_number = 0
        self.playback_timer       = None
        self._updating_slider     = False
        self._updating_combo      = False
        self._timeline_was_playing = False
        self._play_start_time     = 0.0
        self._play_start_frame    = 0
        self._playback_speed      = 1.0
        self._current_shot_end_frame = 0
        self._last_pixmap          = None   # last displayed frame, unscaled — for resize rescaling
        self.audio                = AudioPlayer(verbose=self._verbose)
        self._audio_start_signal.connect(self._on_audio_start_main_thread)

        # ---- Subtitle state ----
        self.subtitle_cues: list  = []

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
            from tool import prefs as _prefs
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
        restore_window_geometry(self, "window_shotlist")
        self.load_first_shot()

        # Start the IPC server so the Metadata Visualizer (and CLI) can
        # tell this window to load a different film without opening a second instance.
        self._ipc_server = _IpcServer(project_path, self)
        self._ipc_server.load_requested.connect(self._on_ipc_load)
        self._ipc_server.start()

    def _on_ipc_load(self, filename: str, media_type: str, shot_id: str, playback: str) -> None:
        """Handle a load request arriving from the IPC socket."""
        self.raise_()
        self.activateWindow()
        if not filename:
            # Empty filename = raise-only ping; nothing else to do.
            return
        if media_type != self.media_type:
            # Switch media type first: update combo + repopulate filenames list,
            # but do NOT auto-load index 0 (we'll load the requested file below).
            self.media_type = media_type
            try:
                from data.metadata import get_metadata as _get_meta
                all_entries = _get_meta(self.project_path, media_type=media_type)
                self.filenames = [e["filename"] for e in all_entries if e.get("filename")]
            except Exception:
                self.filenames = []
            self._updating_combo = True
            self.media_type_combo.blockSignals(True)
            self.media_type_combo.setCurrentText(media_type)
            self.media_type_combo.blockSignals(False)
            self.movie_combo.clear()
            for fn in self.filenames:
                self.movie_combo.addItem(_display_name(fn), fn)
            self._updating_combo = False
            self.current_movie_index = -1
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

        # Jump to the requested shot if one was provided.
        if shot_id:
            for i, shot in enumerate(self.shots):
                if str(shot.get("shot_id", "")) == str(shot_id):
                    self.jump_to_shot(i)
                    break

        # Enforce playback state requested by the caller.
        if playback == "pause" and self.is_playing:
            self.stop_playback()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _open_video(self, fatal: bool = True) -> bool:
        self.video_path = (
            Path(self.project_path) / "media" / "videos"
            / self.media_type / self.filename
        )
        if not self.video_path.exists():
            if fatal:
                QMessageBox.critical(self, "Error", f"Video file not found:\n{self.video_path}")
                sys.exit(1)
            # Runtime switch — clear cap so callers know there is no video
            if self.cap is not None:
                self.cap.release()
            self.cap = None
            self.frame_rate          = 30.0
            self.total_frames        = 0
            self.video_native_width  = 320
            self.video_native_height = 180
            self.sar_num, self.sar_den = 1, 1
            return False
        self.cap = cv2.VideoCapture(str(self.video_path))
        if not self.cap.isOpened():
            if fatal:
                QMessageBox.critical(self, "Error", f"Could not open video:\n{self.video_path}")
                sys.exit(1)
            self.cap = None
            self.frame_rate          = 30.0
            self.total_frames        = 0
            self.video_native_width  = 320
            self.video_native_height = 180
            self.sar_num, self.sar_den = 1, 1
            return False
        self.frame_rate          = self.cap.get(cv2.CAP_PROP_FPS)
        self.total_frames        = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        raw_w                    = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.video_native_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.sar_num, self.sar_den = _get_sar(str(self.video_path))
        self.video_native_width  = int(round(raw_w * self.sar_num / self.sar_den))

    def _load_data(self, fatal_on_missing: bool = True) -> bool:
        """Load shotlist and annotations for the current film.

        Returns True on success. If the shotlist is missing:
        - gameplay: always silently returns empty (no dialog)
        - movie: shows a warning; exits when fatal_on_missing=True (startup),
          or returns False (runtime).
        """
        try:
            self.shots = read_shotlist(self.project_path, self.filename, self.media_type)
        except FileNotFoundError:
            if self.media_type == "gameplay":
                # Gameplay shotlists don't exist yet — silently show an empty table
                self.shots = []
                return False
            msg = (
                f"No shotlist found for:\n{self.filename}\n\n"
                "Run 'crossing shotlist shot detect' first to generate a shotlist."
            )
            QMessageBox.warning(self, "No Shotlist", msg)
            if fatal_on_missing:
                sys.exit(0)
            self.shots = []
            return False
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
        try:
            self.audio_gain_db = float(_meta.get("audio_gain_db", 0.0) or 0.0)
        except (TypeError, ValueError):
            self.audio_gain_db = 0.0
        raw_channel_map = _meta.get("audio_channels")
        self.audio_channels = raw_channel_map if isinstance(raw_channel_map, dict) else None
        attach_shot_ids(self.shots, self.media_id)

        ann_path    = _get_annotation_json_path(self.project_path, self.filename, self.media_type)
        ann_entries = _read_annotation_json(ann_path)
        self._has_ann_file              = ann_path.exists()
        self.annotation_index           = _build_annotation_index(ann_entries)
        self._annotation_entry_index    = _build_entry_index(ann_entries)
        self._embedding_row_index       = _build_embedding_row_index(ann_entries)
        self._embeddings                = None
        self._embeddings_loaded         = False

        # Merge best_frame data from annotation JSON into shotlist dicts so
        # _make_shot_row (and keyboard shortcuts) can access shot["best_frame"].
        self._ann_path                  = ann_path
        self._ann_entries               = ann_entries
        bf_index = _build_best_frame_index(ann_entries)
        for shot in self.shots:
            sid = shot.get("shot_id", "")
            if sid in bf_index:
                shot["best_frame"] = bf_index[sid]
            elif "best_frame" not in shot:
                shot["best_frame"] = None

        # Load subtitle cues (movies only; gameplay has no subtitles)
        if self.media_type == "movie":
            from data.subtitles import load_subtitle_cues as _load_cues
            self.subtitle_cues = _load_cues(
                self.project_path, self.media_type, self.filename
            )
        else:
            self.subtitle_cues = []
        return True

    def _reload_for_movie(self, index: int):
        """Reload video + data for movie *index*, then refresh the UI."""
        self.current_movie_index = index
        self.filename            = self.filenames[index]
        self._edited_shots.clear()
        self._ann_dirty = False
        if self.cap is not None:
            self.cap.release()
        self._open_video(fatal=False)
        self._load_data(fatal_on_missing=False)

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
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        h_splitter = GripSplitter(Qt.Horizontal)
        outer.addWidget(h_splitter, stretch=1)

        # ---- Browser: full-window playback area ----
        browser = self._build_browser()
        h_splitter.addWidget(browser)

        # ---- Scene panel: its own collapsible, content-width side panel ----
        scene_panel = self._build_scene_panel()
        h_splitter.addWidget(scene_panel)

        # ---- Shot panel: its own collapsible, content-width side panel ----
        shot_panel = self._build_shot_panel()
        h_splitter.addWidget(shot_panel)

        # ---- Inspector: Filter / Info / Annotation / Playback / Tools ----
        inspector = self._build_inspector()
        h_splitter.addWidget(inspector)

        # Browser absorbs all extra space; the side panels and inspector are
        # fixed-width and only collapse/expand via their splitter grip handles.
        h_splitter.setStretchFactor(0, 1)
        h_splitter.setStretchFactor(1, 0)
        h_splitter.setStretchFactor(2, 0)
        h_splitter.setStretchFactor(3, 0)
        self._h_splitter   = h_splitter
        self._scene_panel  = scene_panel
        self._shot_panel   = shot_panel

        self.rebuild_shot_list()
        self.rebuild_scene_list()
        self.update_stats()

        QTimer.singleShot(0, self._fit_side_panels)
        self.setFocus()

    def _build_browser(self) -> QWidget:
        """Build the Browser: full-window video playback, subtitle overlay,
        and the bottom timeline scrubber.

        The Browser is for viewing, scrubbing, and navigating playback only —
        annotations and inspector-style metadata live in the Inspector instead.
        """
        browser = QWidget()
        browser_layout = QVBoxLayout(browser)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        browser_layout.setSpacing(2)
        # Same dark display background used by the Book and Illustration
        # browsers, so the Shotlist Browser's video/letterboxing matches the
        # shared framework look instead of the generic app grey.
        browser.setStyleSheet(f"background: {theme.CANVAS_BG};")

        self.frame_label = QLabel()
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setScaledContents(False)
        self.frame_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.frame_label.setMinimumSize(1, 1)
        # Rescale the cached frame whenever the label's size changes for any
        # reason (window resize, splitter drag, or a deferred panel-fit
        # changing pane widths) so the video always fills the available area.
        self.frame_label.installEventFilter(self)

        # Subtitle overlay — sits on top of the video frame, anchored to the
        # bottom, and is never part of the vertical flow so video position is
        # unaffected by subtitle content or height.
        self.subtitle_label = QLabel()
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        self.subtitle_label.setFont(theme.font_subtitle())
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setStyleSheet(
            f"color: {theme.TEXT}; background-color: transparent; padding: 2px 8px;"
            f" font-size: {theme.SUBTITLE_PT}pt;"
        )

        _sub_overlay = QWidget()
        _sub_overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        _sub_overlay.setStyleSheet("background: transparent;")
        _sub_ol = QVBoxLayout(_sub_overlay)
        _sub_ol.setContentsMargins(0, 0, 0, 8)
        _sub_ol.setSpacing(0)
        _sub_ol.addStretch()
        _sub_ol.addWidget(self.subtitle_label)

        video_container = QWidget()
        video_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        _stack = QStackedLayout(video_container)
        _stack.setStackingMode(QStackedLayout.StackAll)
        _stack.setContentsMargins(0, 0, 0, 0)
        _stack.addWidget(self.frame_label)
        _stack.addWidget(_sub_overlay)
        _stack.setCurrentIndex(1)

        browser_layout.addWidget(video_container, stretch=1)

        # Timeline scrubber — a real scrollbar whose handle length reflects
        # the current shot's frame span relative to the whole film's length,
        # the same idea as the Book Visualizer's bottom position bar.
        self.timeline_slider = JumpScrollBar(Qt.Horizontal)
        self.timeline_slider.setMinimum(0)
        self.timeline_slider.setMaximum(max(0, self.total_frames - 1))
        self.timeline_slider.setPageStep(1)
        self.timeline_slider.setValue(0)
        self.timeline_slider.setFixedHeight(theme.SCROLLBAR_W)
        self.timeline_slider.setFocusPolicy(Qt.NoFocus)
        self.timeline_slider.valueChanged.connect(self._on_timeline_seek)
        self.timeline_slider.mousePressed.connect(self._on_timeline_press)
        self.timeline_slider.mouseReleased.connect(self._on_timeline_release)
        self.timeline_slider.setToolTip("Scrub timeline  [←/→ frame  Shift+←/→ 1s]")
        browser_layout.addWidget(self.timeline_slider)

        return browser

    def _build_scene_panel(self) -> QWidget:
        """Build the Scene index as its own collapsible, content-width panel."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

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
        self.scene_list.setFocusPolicy(Qt.NoFocus)
        self.scene_list.cellClicked.connect(self.on_scene_selected)
        self.scene_list.setToolTip("Scenes — click to jump  [PgUp/PgDn navigate  N/M split/merge]")
        self.scene_list.setFrameShape(QFrame.NoFrame)
        self.scene_list.setStyleSheet(_tbl)
        self.scene_list.setVerticalScrollBar(JumpScrollBar())
        layout.addWidget(self.scene_list, stretch=1)

        return panel

    def _build_shot_panel(self) -> QWidget:
        """Build the Shot index + frame-based timecode table as its own
        collapsible, content-width panel."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.shot_list = QTableWidget()
        self.shot_list.setColumnCount(6)
        self.shot_list.setHorizontalHeaderLabels(["\u2713", "Shot", "Start", "Best", "Stop", "Ignore"])
        self.shot_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.shot_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.shot_list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.shot_list.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.shot_list.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.shot_list.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
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
        self.shot_list.setToolTip("Shots \u2014 click to jump\nClick Best to jump to best frame\nclick Stop to show end frame\nb to jump to best\nShift+B set best\nCtrl+B clear best\n\u2191/\u2193 navigate]")
        self.shot_list.cellClicked.connect(self.on_shot_selected)
        # Use a stylesheet WITHOUT any QTableWidget::item rules.
        # Any QTableWidget::item QSS rule (even without background:) causes
        # Qt's QStyleSheetStyle to override item.setBackground() calls.
        # Selection colour is applied via the palette instead.
        _shot_list_stylesheet = f"""
            QTableWidget {{
                background: {theme.TAB_BG};
                border: none;
                gridline-color: {theme.TAB_BG};
            }}
            QTableWidget::item:selected {{
                background: {theme.ACCENT};
                color: {theme.ACCENT_TEXT};
            }}
            QHeaderView::section {{
                background: {theme.PANEL_BG};
                color: {theme.TEXT};
                font-weight: bold;
                border: none;
                padding: 4px 2px;
            }}
            QTableCornerButton::section {{
                background: {theme.PANEL_BG};
                border: none;
            }}
        """
        self.shot_list.setStyleSheet(_shot_list_stylesheet)
        _pal = self.shot_list.palette()
        _pal.setColor(QPalette.Highlight, QColor(theme.ACCENT))
        _pal.setColor(QPalette.HighlightedText, QColor(theme.ACCENT_TEXT))
        self.shot_list.setPalette(_pal)
        self.shot_list.setVerticalScrollBar(JumpScrollBar())
        layout.addWidget(self.shot_list, stretch=1)

        return panel

    def _build_inspector(self) -> QWidget:
        """Build the right-side inspector: a tabbed panel (single "Shotlist" tab)
        containing Filter, Info, Annotation, Playback, and Tools sections.

        Unlike the Scene/Shot panels, the inspector is a genuinely resizable
        splitter pane (drag its grip handle to widen/narrow it). Its minimum
        width is computed from the Tools section's 2-column button grid — the
        widest fixed content in the inspector — so the tab can be as thin as
        possible without any button text overflowing; narrower than that and
        the GripSplitter's usual collapse behavior takes over. It is wrapped
        in a QTabWidget purely for visual consistency with the Book/
        Illustration inspector color scheme, even though only a single tab
        exists today.
        """
        outer = QWidget()
        outer.setStyleSheet(f"background: {theme.PANEL_BG};")

        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.tabBar().setDrawBase(False)
        tabs.setStyleSheet(theme.tab_strip_stylesheet())

        shotlist_tab = QWidget()
        shotlist_tab.setStyleSheet(f"background: {theme.TAB_BG};")
        shotlist_tab_layout = QVBoxLayout(shotlist_tab)
        shotlist_tab_layout.setContentsMargins(0, 0, 0, 0)
        shotlist_tab_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setFocusPolicy(Qt.NoFocus)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Always reserve the vertical scrollbar's width instead of letting it
        # appear/disappear on demand — otherwise the button grids below would
        # reflow every time a section's expand/collapse crosses the scroll
        # threshold.  With AlwaysOn the scrollbar's width is baked into the
        # layout from the start, so it only ever "pushes" the other grip
        # panels once (via _fit_side_panels), never mid-session.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        scroll.setStyleSheet(f"QScrollArea {{ background: {theme.TAB_BG}; border: none; }}")
        shotlist_tab_layout.addWidget(scroll)

        content = QWidget()
        content.setStyleSheet(f"background: {theme.TAB_BG};")
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(
            theme.SECTION_GAP, theme.SECTION_GAP, theme.SECTION_GAP, theme.SECTION_GAP
        )
        layout.setSpacing(theme.SECTION_GAP)
        layout.setAlignment(Qt.AlignTop)

        filter_sec     = self._build_filter_section()
        info_sec       = self._build_info_section()
        annotation_sec = self._build_annotation_section()
        playback_sec   = self._build_playback_section()
        tools_sec      = self._build_tools_section()   # sets self._tools_content_w

        layout.addWidget(filter_sec)
        layout.addWidget(info_sec)
        layout.addWidget(annotation_sec)
        layout.addWidget(playback_sec)
        layout.addWidget(tools_sec)

        tabs.addTab(shotlist_tab, "Shotlist")
        outer_layout.addWidget(tabs)

        # Thinnest allowed tab width = the Tools grid's natural (unwrapped)
        # button width + the content layout's own margins + the permanently
        # reserved vertical scrollbar strip.
        self._inspector_min_w = self._tools_content_w + 2 * theme.SECTION_GAP + theme.SCROLLBAR_W
        outer.setMinimumWidth(self._inspector_min_w)

        return outer

    def _build_filter_section(self) -> CollapsibleSection:
        """Filter — choose which film (and media type) the Browser shows."""
        sec = CollapsibleSection("Filter", pref_key="shotlist_section_filter")

        self.media_type_combo = QComboBox()
        self.media_type_combo.setFocusPolicy(Qt.NoFocus)
        self.media_type_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLength)
        self.media_type_combo.setMinimumContentsLength(8)
        self.media_type_combo.addItems(["movie", "gameplay"])
        self.media_type_combo.setCurrentText(self.media_type)
        self.media_type_combo.currentTextChanged.connect(self._on_media_type_changed)
        sec.add_widget(self.media_type_combo)

        # AdjustToMinimumContentsLength decouples the combo's own width from
        # the length of its longest item — without this, a long movie title
        # would make QComboBox report a huge sizeHint()/minimumSizeHint()
        # that forces the whole Inspector wider.  The full title is shown
        # separately, wrapped, in _movie_title_label below.
        self.movie_combo = QComboBox()
        self.movie_combo.setFocusPolicy(Qt.NoFocus)
        self.movie_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLength)
        self.movie_combo.setMinimumContentsLength(8)
        for fn in self.filenames:
            self.movie_combo.addItem(_display_name(fn), fn)
        self.movie_combo.setCurrentIndex(self.current_movie_index)
        self.movie_combo.currentIndexChanged.connect(self.on_movie_combo_changed)
        sec.add_widget(self.movie_combo)

        # The combo box itself can only elide its closed-state text onto one
        # line; this label shows the full title, wrapping at word/character
        # boundaries when the inspector is narrower than the title.
        self._movie_title_label = QLabel(self.movie_combo.currentText())
        self._movie_title_label.setWordWrap(True)
        self._movie_title_label.setFont(theme.font_mono())
        self._movie_title_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.movie_combo.currentTextChanged.connect(self._movie_title_label.setText)
        sec.add_widget(self._movie_title_label)

        return sec

    def _build_info_section(self) -> CollapsibleSection:
        """Info — aggregate shotlist stats plus the selected shot's detail."""
        sec = CollapsibleSection("Info", pref_key="shotlist_section_info")

        self._stats_block = MetadataBlock(["Scenes", "Shots", "Active", "Ignored", "Annotated"])
        _allow_metadata_wrap(self._stats_block)
        sec.add_widget(self._stats_block)

        self._shot_info_block = MetadataBlock(
            ["Scene", "Shot", "Frame", "Start", "End", "Confidence", "Shot ID"]
        )
        _allow_metadata_wrap(self._shot_info_block)
        sec.add_widget(self._shot_info_block)

        return sec

    def _build_annotation_section(self) -> CollapsibleSection:
        """Annotation — annotation display/edit surface plus annotation actions.

        Annotations live in the Inspector, never in the Browser.
        """
        sec = CollapsibleSection("Annotation", pref_key="shotlist_section_annotation")

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(theme.SECTION_GAP)

        self.annotate_button = QPushButton("\u26a1 Auto-Annotate")
        self.annotate_button.setCheckable(True)
        self.annotate_button.setChecked(False)
        self.annotate_button.clicked.connect(self._toggle_auto_annotate)
        self.annotate_button.setFocusPolicy(Qt.NoFocus)
        self.annotate_button.setStyleSheet(theme.action_button_stylesheet())
        self.annotate_button.setToolTip(
            "Start / stop background LLM annotation of unannotated shots in this film"
        )
        btn_row.addWidget(self.annotate_button)

        self.remove_ann_button = QPushButton("\U0001f5d1 Remove")
        self.remove_ann_button.clicked.connect(self._remove_current_annotation)
        self.remove_ann_button.setFocusPolicy(Qt.NoFocus)
        self.remove_ann_button.setStyleSheet(theme.action_button_stylesheet())
        self.remove_ann_button.setToolTip("Delete the annotation for the currently selected shot")
        btn_row.addWidget(self.remove_ann_button)

        btn_row_widget = QWidget()
        btn_row_widget.setLayout(btn_row)
        sec.add_widget(btn_row_widget)

        # This row of paired buttons is one of the candidates considered when
        # computing the Inspector's minimum width (see _build_inspector) —
        # it must never be narrower than this, or the buttons would overflow
        # off the right edge with no way to reach them (no horizontal scroll).
        self._annotation_btn_row_w = (
            self.annotate_button.sizeHint().width()
            + theme.SECTION_GAP
            + self.remove_ann_button.sizeHint().width()
        )

        # The representation combo (fields/json/txt/vector/mapping) is kept
        # fully functional for internal state (_update_annotation_panel etc.)
        # but hidden from view — the fields table below is the only
        # representation shown in the Inspector.
        self.ann_repr_combo = QComboBox()
        self.ann_repr_combo.setFocusPolicy(Qt.NoFocus)
        for _mode in ("fields", "json", "txt", "vector", "mapping"):
            self.ann_repr_combo.addItem(_mode)
        self.ann_repr_combo.setCurrentIndex(0)
        self.ann_repr_combo.currentIndexChanged.connect(self._on_repr_changed)
        sec.add_widget(self.ann_repr_combo)
        self.ann_repr_combo.hide()

        self.ann_display = QTextEdit()
        self.ann_display.setReadOnly(True)
        self.ann_display.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.ann_display.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.ann_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.ann_display.textChanged.connect(self._on_ann_text_changed)
        self.ann_display.installEventFilter(self)
        self.ann_display.hide()  # hidden when mode == "fields"
        sec.add_widget(self.ann_display)

        # Two-column key/value table (field name | content), matching the
        # same CELL_BG/TEXT_DIM/TEXT color convention as the Book/Illustration
        # Info panels (visualizers/components/metadata_block.py). The value
        # column is still directly editable (double-click), unlike a plain
        # MetadataBlock, so a QTableWidget is used instead of that component.
        self.ann_fields_table = QTableWidget()
        self.ann_fields_table.setColumnCount(2)
        self.ann_fields_table.horizontalHeader().hide()
        self.ann_fields_table.verticalHeader().hide()
        self.ann_fields_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.ann_fields_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.ann_fields_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.ann_fields_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.ann_fields_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.ann_fields_table.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.SelectedClicked |
            QAbstractItemView.EditKeyPressed
        )
        self.ann_fields_table.setWordWrap(True)
        self.ann_fields_table.setFont(theme.font_mono())
        self.ann_fields_table.setStyleSheet(f"""
            QTableWidget {{
                background: {theme.TAB_BG};
                border: none;
                gridline-color: {theme.TAB_BG};
            }}
            QTableWidget::item:selected {{
                background: {theme.ACCENT};
                color: {theme.ACCENT_TEXT};
            }}
        """)
        self.ann_fields_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.ann_fields_table.itemChanged.connect(self._on_fields_cell_changed)
        # Re-fit row heights + the table's own height whenever the column is
        # resized (e.g. inspector width change) — no internal scrollbar; the
        # table always shows every row and pushes the sections below it,
        # letting the outer Inspector scroll area handle any overflow.
        self.ann_fields_table.horizontalHeader().sectionResized.connect(
            self._on_ann_fields_table_resized
        )
        sec.add_widget(self.ann_fields_table)

        self.ann_dirty_label = QLabel()
        self.ann_dirty_label.setFont(theme.font_ui())
        self.ann_dirty_label.setStyleSheet(f"color: {theme.ACCENT};")
        self.ann_dirty_label.setWordWrap(True)
        self.ann_dirty_label.hide()
        sec.add_widget(self.ann_dirty_label)

        return sec

    def _build_playback_section(self) -> CollapsibleSection:
        """Playback — transport controls for the Browser."""
        sec = CollapsibleSection("Playback", pref_key="shotlist_section_playback")
        sec.setToolTip(
            "\u2191 / \u2193  previous / next shot\n"
            "PgUp / PgDn  previous / next scene\n"
            "Space  play / pause\n"
            "\u2190 / \u2192  step one frame\n"
            "Shift+\u2190 / Shift+\u2192  step 1 second\n"
            "Home / End  previous / next movie"
        )

        grid = QGridLayout()
        grid.setSpacing(theme.SECTION_GAP)

        self.play_pause_button = QPushButton("\u25b6 Play")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.play_pause_button.setFocusPolicy(Qt.NoFocus)
        self.play_pause_button.setStyleSheet(theme.action_button_stylesheet())
        self.play_pause_button.setToolTip("Play / Pause  [Space]")
        grid.addWidget(self.play_pause_button, 0, 0)

        self.continue_button = QPushButton("Continue")
        self.continue_button.setCheckable(True)
        self.continue_button.setChecked(True)
        self.continue_button.clicked.connect(self._on_continue_clicked)
        self.continue_button.setFocusPolicy(Qt.NoFocus)
        self.continue_button.setStyleSheet(theme.action_button_stylesheet())
        self.continue_button.setToolTip("When OFF: playback stops at the end of the current shot  [C]")
        grid.addWidget(self.continue_button, 0, 1)

        self.loop_button = QPushButton("Loop")
        self.loop_button.setCheckable(True)
        self.loop_button.setChecked(False)
        self.loop_button.clicked.connect(self._on_loop_clicked)
        self.loop_button.setFocusPolicy(Qt.NoFocus)
        self.loop_button.setStyleSheet(theme.action_button_stylesheet())
        self.loop_button.setToolTip("Loop the current shot infinitely until turned off  [L]")
        grid.addWidget(self.loop_button, 1, 0)

        self.gremlins_button = QPushButton("\U0001f47e Gremlins")
        self.gremlins_button.setCheckable(True)
        self.gremlins_button.setChecked(False)
        self.gremlins_button.clicked.connect(self.toggle_gremlins)
        self.gremlins_button.setFocusPolicy(Qt.NoFocus)
        self.gremlins_button.setStyleSheet(theme.action_button_stylesheet())
        self.gremlins_button.setToolTip("Randomly jump movies/timecodes every 5 s  [G]")
        grid.addWidget(self.gremlins_button, 1, 1)

        grid_widget = QWidget()
        grid_widget.setLayout(grid)
        sec.add_widget(grid_widget)

        # This 2-column grid is one of the candidates considered when
        # computing the Inspector's minimum width (see _build_inspector).
        col0_w = max(self.play_pause_button.sizeHint().width(), self.loop_button.sizeHint().width())
        col1_w = max(self.continue_button.sizeHint().width(), self.gremlins_button.sizeHint().width())
        self._playback_content_w = col0_w + theme.SECTION_GAP + col1_w

        return sec

    def _build_tools_section(self) -> CollapsibleSection:
        """Tools — operations that change project data (shot/scene structure)."""
        sec = CollapsibleSection("Tools", pref_key="shotlist_section_tools")
        sec.setToolTip(
            "I  toggle Ignore on current shot\n"
            "N  new scene at current shot\n"
            "Shift+N  new shot at current frame\n"
            "M  merge current scene into previous\n"
            "Shift+M  merge current shot with previous\n"
            "Ctrl+S  save shotlist changes\n"
            "\n"
            "Scene table — click a row to jump to that scene\n"
            "Shot table — click a row to jump to that shot\n"
            "  click Best to jump to the best frame\n"
            "  click Stop to show the end frame\n"
            "B  jump to best frame\n"
            "Shift+B  set current frame as best\n"
            "Ctrl+B  clear best frame"
        )

        grid = QGridLayout()
        grid.setSpacing(theme.SECTION_GAP)

        self.split_button = QPushButton("New Shot")
        self.split_button.clicked.connect(self.split_shot_at_current_frame)
        self.split_button.setFocusPolicy(Qt.NoFocus)
        self.split_button.setStyleSheet(theme.action_button_stylesheet())
        self.split_button.setToolTip("Split current shot at current frame  [Shift+N]")
        grid.addWidget(self.split_button, 0, 0)

        self.merge_button = QPushButton("Merge Shot")
        self.merge_button.clicked.connect(self.merge_with_previous)
        self.merge_button.setFocusPolicy(Qt.NoFocus)
        self.merge_button.setStyleSheet(theme.action_button_stylesheet())
        self.merge_button.setToolTip("Merge current shot with previous shot  [Shift+M]")
        grid.addWidget(self.merge_button, 0, 1)

        self.ignore_button = QPushButton("\u2297 Ignore")
        self.ignore_button.clicked.connect(self.toggle_current_ignore)
        self.ignore_button.setFocusPolicy(Qt.NoFocus)
        self.ignore_button.setStyleSheet(theme.action_button_stylesheet())
        self.ignore_button.setToolTip("Toggle Ignore on current shot  [I]")
        grid.addWidget(self.ignore_button, 1, 0)

        self.split_scene_button = QPushButton("New Scene")
        self.split_scene_button.clicked.connect(self.split_scene_at_current_shot)
        self.split_scene_button.setFocusPolicy(Qt.NoFocus)
        self.split_scene_button.setStyleSheet(theme.action_button_stylesheet())
        self.split_scene_button.setToolTip("Start a new scene at current shot  [N]")
        grid.addWidget(self.split_scene_button, 1, 1)

        self.merge_scene_button = QPushButton("Merge Scene")
        self.merge_scene_button.clicked.connect(self.merge_scene_at_current_shot)
        self.merge_scene_button.setFocusPolicy(Qt.NoFocus)
        self.merge_scene_button.setStyleSheet(theme.action_button_stylesheet())
        self.merge_scene_button.setToolTip("Merge current scene into previous scene  [M]")
        grid.addWidget(self.merge_scene_button, 2, 0)

        self.save_button = QPushButton("\U0001f4be Save")
        self.save_button.clicked.connect(self.save_changes)
        self.save_button.setEnabled(False)
        self.save_button.setFocusPolicy(Qt.NoFocus)
        self.save_button.setStyleSheet(theme.action_button_stylesheet())
        self.save_button.setToolTip("Save shotlist changes to CSV  [Ctrl+S]")
        grid.addWidget(self.save_button, 2, 1)

        grid_widget = QWidget()
        grid_widget.setLayout(grid)
        sec.add_widget(grid_widget)

        # The Tools grid (2 columns) is the widest fixed content in the
        # inspector — its natural (unwrapped) button width is the basis for
        # the thinnest allowed Shotlist tab width (see _build_inspector).
        col0_w = max(
            self.split_button.sizeHint().width(),
            self.ignore_button.sizeHint().width(),
            self.merge_scene_button.sizeHint().width(),
        )
        col1_w = max(
            self.merge_button.sizeHint().width(),
            self.split_scene_button.sizeHint().width(),
            self.save_button.sizeHint().width(),
        )
        self._tools_content_w = col0_w + theme.SECTION_GAP + col1_w

        return sec

    def _fit_side_panels(self):
        """Auto-size the Scene and Shot panels to the natural width of their content.

        Both panels are fixed-width (content-driven) and are not manually
        resizable — only their splitter grip handles can collapse/expand them.
        """
        self.scene_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.scene_list.resizeColumnsToContents()
        scene_w = self.scene_list.columnWidth(0)
        scene_w += self.scene_list.verticalScrollBar().sizeHint().width()
        self.scene_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        scene_panel_w = max(56, scene_w + 4)
        self._scene_panel.setFixedWidth(scene_panel_w)

        hdr = self.shot_list.horizontalHeader()
        for c in range(self.shot_list.columnCount()):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.shot_list.resizeColumnsToContents()
        shot_w = sum(self.shot_list.columnWidth(c) for c in range(self.shot_list.columnCount()))
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        shot_w += self.shot_list.verticalScrollBar().sizeHint().width()
        shot_panel_w = shot_w + 4
        self._shot_panel.setFixedWidth(shot_panel_w)

        # A QSplitter only re-applies a pane's min/max constraints on the next
        # explicit setSizes() call — it does NOT proactively shrink a pane's
        # already-allocated slot just because a child's setFixedWidth changed
        # after the splitter's first layout pass.  Without this, the handle
        # stays where it was and leaves dead space between the narrowed panel
        # and the next pane.  Force an immediate redistribution here so the
        # Scene/Shot panels and Browser all reflect their real widths right away.
        # The Inspector opens at its thinnest natural (content-driven) width —
        # the user can drag it wider from there.
        total = sum(self._h_splitter.sizes()) or self.width()
        inspector_w = self._inspector_min_w
        browser_w = max(200, total - scene_panel_w - shot_panel_w - inspector_w)
        self._h_splitter.setSizes([browser_w, scene_panel_w, shot_panel_w, inspector_w])

    def _toggle_inspectors(self) -> None:
        """Toggle between full-Browser mode and inspector-visible mode.

        Mirrors the Book/Illustration Visualizers' Tab behavior: hide/show
        the Scene panel, Shot panel, and Inspector as a single operation so
        the Browser can fill the entire window for distraction-free playback.
        """
        sizes = list(self._h_splitter.sizes())
        if len(sizes) != 4:
            return
        if self._inspectors_hidden:
            restore = self._saved_inspector_sizes
            if restore and len(restore) == 4:
                self._h_splitter.setSizes(restore)
            else:
                self._fit_side_panels()
            self._inspectors_hidden = False
        else:
            self._saved_inspector_sizes = sizes
            self._h_splitter.setSizes([max(1, sum(sizes)), 0, 0, 0])
            self._inspectors_hidden = True

    def _sync_timeline_pagestep(self):
        """Set the timeline scrubber's pageStep to the current shot's frame span.

        This makes the scrubber's rendered handle length communicate how much
        of the whole film that shot occupies — mirrors the Book Visualizer's
        bottom position bar.
        """
        if 0 <= self.current_shot_index < len(self.shots):
            shot  = self.shots[self.current_shot_index]
            span  = int(shot.get('end_frame', 0)) - int(shot.get('start_frame', 0)) + 1
            self.timeline_slider.setPageStep(max(1, span))

    def _update_timeline_slider(self):
        """Sync the timeline slider to the current frame without triggering a seek."""
        self._updating_slider = True
        self._sync_timeline_pagestep()
        self.timeline_slider.setValue(self.current_frame_number)
        self._updating_slider = False

    def _on_timeline_press(self):
        """Mouse pressed on timeline — remember play state and pause."""
        self._timeline_was_playing = self.is_playing
        if self.is_playing:
            self.stop_playback()

    def _on_timeline_release(self):
        """Mouse released on timeline — resume playback if we were playing."""
        if self._timeline_was_playing:
            self._timeline_was_playing = False
            self.start_playback()

    def _on_timeline_seek(self, value: int):
        """Handle timeline slider movement — seek to that frame."""
        if self._updating_slider:
            return
        if self.is_playing:
            self.stop_playback()
        self.current_frame_number = value
        self.update_current_shot_from_frame()
        self._sync_timeline_pagestep()
        frame = self._get_frame(self.current_frame_number)
        if frame is not None:
            self._display_frame(frame)
        self.update_frame_info()
        self._update_subtitle_display()

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
            # Copy (not just reference) the QImage-backed pixmap so it survives
            # after frame_rgb's underlying numpy buffer is reused/freed, then
            # cache it unscaled so resize events can rescale without re-decoding.
            self._last_pixmap = pixmap.copy()
            self._rescale_current_frame()

    def _rescale_current_frame(self):
        """Re-scale the last displayed frame to the video label's current size.

        Called after every new frame is displayed, and also whenever
        frame_label itself is resized (window resize, splitter drag, or a
        deferred panel-fit changing pane widths) via the installed event
        filter, so the video always fills the available area instead of
        staying cached at whatever size it happened to be first displayed at.
        """
        if self._last_pixmap is None or self._last_pixmap.isNull():
            return
        scaled_pixmap = self._last_pixmap.scaled(
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
        self.audio.play(
            str(self.video_path),
            start_secs,
            gain_db=getattr(self, "audio_gain_db", 0.0),
            channel_map=getattr(self, "audio_channels", None),
            on_start_callback=self._audio_start_signal,
        )

    def _on_audio_start_main_thread(self, audio_start_time: float, start_secs: float):
        """Anchor video clock to actual audio start time — called on main thread via signal."""
        if not self.is_playing:
            return
        self._play_start_time = audio_start_time
        self._play_start_frame = int(start_secs * self.frame_rate)
        self.playback_timer.start()
    
    def stop_playback(self):
        """Stop video playback."""
        self.is_playing = False
        self.play_pause_button.setText("▶ Play")
        self.playback_timer.stop()
        self.audio.stop()

    def _on_continue_clicked(self):
        """Mutual exclusion: turning Continue on disables Loop."""
        if self.continue_button.isChecked():
            self.loop_button.setChecked(False)

    def _on_loop_clicked(self):
        """Mutual exclusion: turning Loop on disables Continue."""
        if self.loop_button.isChecked():
            self.continue_button.setChecked(False)

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
        speed        = getattr(self, "_playback_speed", 1.0)
        target_frame = self._play_start_frame + int(elapsed * self.frame_rate * speed)
        target_frame = max(0, min(target_frame, self.total_frames - 1))

        if target_frame <= self.current_frame_number:
            return

        self.current_frame_number = target_frame

        if self.current_frame_number >= self.total_frames:
            self.stop_playback()
            return

        # Loop: when Loop is on and we reach the end of the current shot, restart from shot start
        if self.loop_button.isChecked():
            if 0 <= self.current_shot_index < len(self.shots):
                end_frame = int(self.shots[self.current_shot_index].get('end_frame', self.total_frames - 1))
                if self.current_frame_number > end_frame:
                    start_frame = int(self.shots[self.current_shot_index].get('start_frame', 0))
                    self.stop_playback()
                    self.current_frame_number = start_frame
                    self.start_playback()
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
                old.setBackground(item.background())

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

    def _on_media_type_changed(self, media_type: str) -> None:
        """Switch between movie and gameplay title lists."""
        if media_type == self.media_type:
            return
        self.media_type = media_type
        try:
            from data.metadata import get_metadata as _get_meta
            all_entries = _get_meta(self.project_path, media_type=media_type)
            new_filenames = [e["filename"] for e in all_entries if e.get("filename")]
        except Exception:
            new_filenames = []
        self.filenames = new_filenames if new_filenames else []
        self._updating_combo = True
        self.movie_combo.clear()
        for fn in self.filenames:
            self.movie_combo.addItem(_display_name(fn), fn)
        self._updating_combo = False
        if self.filenames:
            self.current_movie_index = -1
            self.switch_to_movie(0)

    def on_movie_combo_changed(self, index: int):
        """Handle movie selection from the dropdown."""
        if self._updating_combo:
            return
        self.switch_to_movie(index)

    def switch_to_movie(self, index: int):
        """Switch to a different movie in the playlist."""
        if index == self.current_movie_index:
            return

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
        """Load the first shot, or frame 0 of the video when no shots exist."""
        if self.shots:
            self.ann_repr_combo.setEnabled(True)
            self.jump_to_shot(0)
        else:
            # No shotlist data — grey out the annotation panel and clear its contents.
            self.ann_repr_combo.setEnabled(False)
            self.ann_display.hide()
            self.ann_fields_table.show()
            self.ann_fields_table.clearContents()
            self.ann_fields_table.setRowCount(0)
            if self.cap and self.cap.isOpened():
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = self.cap.read()
                if ret:
                    self._display_frame(frame)
    
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

    def _seek_to_frame(self, frame_number: int):
        """Seek the player to an arbitrary frame number without changing current_shot_index."""
        if self.is_playing:
            self.stop_playback()
        self.current_frame_number = max(0, min(frame_number, self.total_frames - 1))
        self._update_timeline_slider()
        frame = self._get_frame(self.current_frame_number)
        if frame is not None:
            self._display_frame(frame)
        self.update_frame_info()

    def _persist_best_frame(self, shot: dict) -> None:
        """Write the shot's current best_frame value to the annotation JSON on disk."""
        shot_id = shot.get("shot_id", "")
        if not shot_id:
            return
        path = self._ann_path
        if path is None:
            return
        try:
            entries = _read_annotation_json(path)
            entry_idx = self._annotation_entry_index.get(shot_id)
            if entry_idx is not None and 0 <= entry_idx < len(entries):
                entries[entry_idx]["shot"]["best_frame"] = shot.get("best_frame")
                path.write_text(
                    json.dumps(entries, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
    
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
    
    def _update_subtitle_display(self):
        """Refresh the subtitle label for the current playback position."""
        if not hasattr(self, "subtitle_label"):
            return
        if not self.subtitle_cues or self.frame_rate <= 0:
            self.subtitle_label.setText("")
            return
        from data.subtitles import active_subtitle as _active_subtitle
        secs = self.current_frame_number / self.frame_rate
        text = _active_subtitle(self.subtitle_cues, secs)
        self.subtitle_label.setText(text)

    def update_frame_info(self):
        """Update the Info section's shot-detail block with current frame details."""
        if not (0 <= self.current_shot_index < len(self.shots)):
            return
        shot       = self.shots[self.current_shot_index]
        scene      = shot.get('Scene', '0')
        start_tc   = shot.get('start_time', '?')
        end_tc     = shot.get('end_time', '?')
        confidence = shot.get('Shot_Confidence', '')
        shot_id    = shot.get('shot_id', '')
        self._shot_info_block.set("Scene", str(scene))
        self._shot_info_block.set("Shot", f"#{self.current_shot_index}")
        self._shot_info_block.set("Frame", str(self.current_frame_number))
        self._shot_info_block.set("Start", str(start_tc))
        self._shot_info_block.set("End", str(end_tc))
        self._shot_info_block.set("Confidence", str(confidence) if confidence else "—")
        self._shot_info_block.set("Shot ID", str(shot_id) if shot_id else "—")
        self._update_subtitle_display()
    
    def on_shot_selected(self, row: int, col: int = 0):
        """Handle shot selection from table.

        Col 3 (Best) jumps to the best frame.
        Col 4 (Stop) shows the end frame.
        """
        if col == _BEST_COLUMN_INDEX:
            # Jump to best frame of the clicked row without changing current shot
            self.jump_to_shot(row)
            if 0 <= row < len(self.shots):
                shot = self.shots[row]
                bf = shot.get("best_frame")
                if bf and isinstance(bf, dict):
                    frame_num = bf.get("frame")
                    if frame_num is not None:
                        self._seek_to_frame(int(frame_num))
            return
        self.jump_to_shot(row, show_end=(col == 4))
    
    def toggle_current_ignore(self):
        """Toggle ignore status for current shot."""
        if 0 <= self.current_shot_index < len(self.shots):
            shot = self.shots[self.current_shot_index]
            shot['Ignore'] = 'No' if shot.get('Ignore', 'No') == 'Yes' else 'Yes'
            self._refresh_shot_row(self.current_shot_index)
            self._mark_dirty()
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
        
        self._mark_dirty()
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

        self._mark_dirty()
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
        self._mark_dirty()
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
        self._mark_dirty()
        self.update_stats()

    def update_stats(self):
        """Update the Info section's aggregate stats block."""
        total_shots   = len(self.shots)
        ignored_shots = sum(1 for shot in self.shots if shot.get('Ignore', 'No') == 'Yes')
        active_shots  = total_shots - ignored_shots
        total_scenes  = len(set(shot.get('Scene', '0') for shot in self.shots))
        ann_count     = len(self.annotation_index)
        ann_str       = f"{ann_count}/{total_shots}" if self._has_ann_file else "—"
        self._stats_block.set("Scenes", str(total_scenes))
        self._stats_block.set("Shots", str(total_shots))
        self._stats_block.set("Active", str(active_shots))
        self._stats_block.set("Ignored", str(ignored_shots))
        self._stats_block.set("Annotated", ann_str)
    
    def update_buttons(self):
        """Update button states based on current position."""
        self.merge_button.setEnabled(self.current_shot_index > 0)
    
    def save_changes(self):
        """Save modified shotlist. Shows an error dialog only on failure."""
        try:
            write_shotlist(self.project_path, self.filename, self.media_type, self.shots)
            self.modified = False
            self.save_button.setEnabled(False)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save shotlist:\n{e}")

    def _mark_dirty(self):
        """Mark shotlist as modified and immediately auto-save."""
        self.modified = True
        self.save_button.setEnabled(True)
        self.save_changes()
    
    def eventFilter(self, obj, event):
        """Intercept events from child widgets to handle keyboard shortcuts globally
        and to keep the Browser's video frame filling its label after any resize."""
        try:
            if event.type() == QEvent.Resize and obj is self.frame_label:
                self._rescale_current_frame()
                return False
            if event.type() == QEvent.Resize and obj is self.ann_display:
                # Re-fit the annotation text display's height to its wrapped
                # content whenever the inspector is resized (e.g. drag-resize
                # of the Shotlist tab) — it never scrolls internally.
                self._resize_ann_display()
                return False
            if event.type() == QEvent.KeyPress:
                key  = event.key()
                mods = event.modifiers()
                if obj == self.ann_display:
                    # Ctrl+S saves a dirty JSON edit; Escape discards it (or,
                    # when there's nothing to discard, closes the window like
                    # everywhere else). QTextEdit consumes these before they
                    # reach keyPressEvent, so we must intercept them here.
                    if key == Qt.Key_S and mods & Qt.ControlModifier:
                        if self._ann_dirty:
                            self._save_annotation_edit()
                        return True
                    if key == Qt.Key_Escape:
                        if self._ann_dirty:
                            self._discard_ann_edit()
                        else:
                            self.close()
                        return True
                    if key in (Qt.Key_Q, Qt.Key_W) and mods & Qt.ControlModifier:
                        self.close()
                        return True
                    if key in (Qt.Key_Tab, Qt.Key_Backtab):
                        # Redirect Tab / Shift+Tab to the window instead of
                        # letting the text edit insert a tab character.
                        self.keyPressEvent(event)
                        return True
                elif obj == self.shot_list:
                    # Redirect keyboard events to main window
                    if key in (Qt.Key_Q, Qt.Key_W) and mods & Qt.ControlModifier:
                        self.close()
                        return True
                    if key in (KEY_PLAY_PAUSE, KEY_PREV_FRAME, KEY_NEXT_FRAME,
                               KEY_PREV_SHOT, KEY_NEXT_SHOT,
                               KEY_PREV_ITEM, KEY_NEXT_ITEM,
                               KEY_PREV_TITLE, KEY_NEXT_TITLE,
                               Qt.Key_B, Qt.Key_C, Qt.Key_E, Qt.Key_F, Qt.Key_I, Qt.Key_M, Qt.Key_N, Qt.Key_G, Qt.Key_S,
                               Qt.Key_Tab, Qt.Key_Backtab, Qt.Key_Escape):
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
        if key == Qt.Key_Escape:
            self.close()
            return
        if key in (Qt.Key_Backtab, Qt.Key_Tab) and mods & Qt.ShiftModifier and not (
            mods & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)
        ):
            # Shift+Tab — toggle true fullscreen
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            return
        if key == Qt.Key_Tab and not (
            mods & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier | Qt.ShiftModifier)
        ):
            # Tab — toggle Scene/Shot/Inspector panes
            self._toggle_inspectors()
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
        elif key == KEY_PREV_SHOT:
            self.prev_shot()
        elif key == KEY_NEXT_SHOT:
            self.next_shot()
        elif key == KEY_PREV_ITEM:
            self.prev_scene()
        elif key == KEY_NEXT_ITEM:
            self.next_scene()
        elif key == Qt.Key_E:
            self.show_end_frame()
        elif key == Qt.Key_C:
            new_state = not self.continue_button.isChecked()
            self.continue_button.setChecked(new_state)
            if new_state:
                self.loop_button.setChecked(False)
        elif key == Qt.Key_L:
            new_state = not self.loop_button.isChecked()
            self.loop_button.setChecked(new_state)
            if new_state:
                self.continue_button.setChecked(False)
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
        elif key == Qt.Key_B:
            shot = self.shots[self.current_shot_index] if 0 <= self.current_shot_index < len(self.shots) else None
            if shot is not None:
                if mods & Qt.ControlModifier:
                    # Ctrl+B — clear best frame
                    shot["best_frame"] = None
                    self._persist_best_frame(shot)
                    self._refresh_shot_row(self.current_shot_index)
                elif mods & Qt.ShiftModifier:
                    # Shift+B — set current frame as user best frame
                    from services.frame_match import set_best_frame as _set_bf
                    _set_bf(shot, self.current_frame_number, source="user", score=1.0, method="manual")
                    self._persist_best_frame(shot)
                    self._refresh_shot_row(self.current_shot_index)
                else:
                    # B — jump to best frame
                    bf = shot.get("best_frame")
                    if bf and isinstance(bf, dict):
                        frame_num = bf.get("frame")
                        if frame_num is not None:
                            self._seek_to_frame(int(frame_num))
        elif key == KEY_PREV_TITLE:
            if self.current_movie_index > 0:
                self.switch_to_movie(self.current_movie_index - 1)
        elif key == KEY_NEXT_TITLE:
            if self.current_movie_index < len(self.filenames) - 1:
                self.switch_to_movie(self.current_movie_index + 1)
        else:
            super().keyPressEvent(event)
    
    def closeEvent(self, event):
        """Handle window close."""
        save_window_geometry(self, "window_shotlist")
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
            self.save_changes()

        event.accept()


    # ------------------------------------------------------------------ #
    #  Annotation panel                                                    #
    # ------------------------------------------------------------------ #

    def _resize_ann_fields_table(self) -> None:
        """Size ann_fields_table to exactly fit all of its rows.

        No internal scrollbar — the table always shows every row and grows
        or shrinks the Annotation section, pushing Playback/Tools down (or
        back up), while the outer Inspector scroll area handles any overflow.
        """
        tbl = self.ann_fields_table
        total_h = sum(tbl.rowHeight(r) for r in range(tbl.rowCount()))
        tbl.setFixedHeight(total_h + 2 * tbl.frameWidth())

    def _on_ann_fields_table_resized(self, _logical_index, _old_size, _new_size) -> None:
        """Re-fit row heights and the table's own height after a width change."""
        self.ann_fields_table.resizeRowsToContents()
        self._resize_ann_fields_table()

    def _resize_ann_display(self) -> None:
        """Size ann_display to exactly fit its wrapped document content.

        No internal scrollbar — the widget always shows all of its text and
        grows or shrinks the Annotation section, pushing Playback/Tools down
        (or back up), while the outer Inspector scroll area handles any
        overflow.
        """
        width = self.ann_display.viewport().width()
        if width <= 0:
            return
        doc = self.ann_display.document()
        doc.setTextWidth(width)
        height = int(doc.size().height()) + 2 * self.ann_display.frameWidth()
        self.ann_display.setFixedHeight(max(1, height))

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
            self._resize_ann_display()
        self._ann_dirty = False
        self.ann_dirty_label.hide()

    def _populate_fields_table(self, ann: dict | None, shot: dict):
        """Fill ann_fields_table with one key/value row per annotation field."""
        tbl = self.ann_fields_table
        tbl.blockSignals(True)
        tbl.clearContents()
        if tbl.columnSpan(0, 0) > 1:
            tbl.setSpan(0, 0, 1, 1)   # clear a previous "no annotation" span
        if ann is None:
            tbl.setRowCount(1)
            item = QTableWidgetItem("(no annotation)")
            item.setFlags(Qt.ItemIsEnabled)
            item.setBackground(QColor(theme.CELL_BG))
            item.setForeground(QColor(theme.TEXT_DIM))
            tbl.setItem(0, 0, item)
            tbl.setSpan(0, 0, 1, 2)
            tbl.blockSignals(False)
            self._resize_ann_fields_table()
            return

        try:
            from data.index import load_fields
            ordered_keys = load_fields(self.project_path)
        except Exception:
            ordered_keys = [k for k in ann if k != "shot_index"]
        keys = [k for k in ordered_keys if k in ann]

        tbl.setRowCount(len(keys))
        cell_bg = QColor(theme.CELL_BG)
        key_fg  = QColor(theme.TEXT_DIM)
        val_fg  = QColor(theme.TEXT)

        for row, k in enumerate(keys):
            # ---- key column (non-editable) ----
            key_item = QTableWidgetItem(k)
            key_item.setFont(theme.font_ui())
            key_item.setBackground(cell_bg)
            key_item.setForeground(key_fg)
            key_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            key_item.setFlags(Qt.ItemIsEnabled)
            tbl.setItem(row, 0, key_item)

            # ---- value column (editable) ----
            v = ann[k]
            v_str = ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
            value_item = QTableWidgetItem(v_str)
            value_item.setBackground(cell_bg)
            value_item.setForeground(val_fg)
            value_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            value_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable | Qt.ItemIsSelectable)
            tbl.setItem(row, 1, value_item)

        tbl.resizeRowsToContents()
        tbl.blockSignals(False)
        self._resize_ann_fields_table()

    def _on_fields_cell_changed(self, item: "QTableWidgetItem"):
        """Called when a content cell in the fields table is edited."""
        if item.column() != 1:
            return   # only the value column is editable
        row = item.row()

        key_item = self.ann_fields_table.item(row, 0)
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
        self._resize_ann_fields_table()

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
        try:
            self._embeddings = load_embeddings(self.project_path, self.filename, self.media_type)
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
        self._resize_ann_display()
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
    parser.add_argument('--media', choices=['movie', 'gameplay'], default='movie')
    parser.add_argument('--project', help="Project path (default: current directory)")
    parser.add_argument('--filenames', nargs='+', help="Explicit list of filenames (passed by cli.py)")
    parser.add_argument('--all', action='store_true', help="Validate all movies with shotlists")
    parser.add_argument('--verbose', action='store_true', help="Print audio/gremlins diagnostics to stderr (also writes a crash log)")
    parser.add_argument('--shot-id', dest='shot_id', default='', help="Jump to this shot_id on startup")
    parser.add_argument('--play', action='store_true', help="Start playback immediately on startup")
    parser.add_argument('--loop', action='store_true', help="Enable loop mode on startup")
    parser.add_argument('--no-continue', dest='no_continue', action='store_true', help="Disable continue mode on startup")

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

    # Verify shotlists exist (skip for gameplay — visualizer shows graceful message)
    if args.media != "gameplay":
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

    # Use saved geometry if available; otherwise open maximised.
    from tool import prefs as _prefs
    if not isinstance(_prefs.get("window_shotlist"), (list, tuple)):
        screen = QApplication.primaryScreen()
        visualizer.setGeometry(screen.availableGeometry())
    visualizer.show()
    QTimer.singleShot(0, lambda: (visualizer.raise_(), visualizer.activateWindow()))

    # Apply initial button states from CLI args
    if getattr(args, 'no_continue', False):
        visualizer.continue_button.setChecked(False)
    if getattr(args, 'loop', False):
        visualizer.loop_button.setChecked(True)
        visualizer.continue_button.setChecked(False)

    # Jump to a specific shot if requested (e.g. launched from the Mosaic Visualizer).
    if args.shot_id:
        _target_shot_id = args.shot_id
        def _jump_on_start():
            for i, shot in enumerate(visualizer.shots):
                if str(shot.get("shot_id", "")) == _target_shot_id:
                    visualizer.jump_to_shot(i)
                    if visualizer.is_playing:
                        visualizer.stop_playback()
                    break
        QTimer.singleShot(250, _jump_on_start)

    # Start playback if requested (slightly after shot jump to ensure seek is complete)
    if getattr(args, 'play', False):
        QTimer.singleShot(300, visualizer.start_playback)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()


# Backward-compat alias — legacy code that imports OpenCVShotVisualizer still works
OpenCVShotVisualizer = ShotlistVisualizer
