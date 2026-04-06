#!/usr/bin/env python3
"""Text Validator - Frame-precise on-screen text event validation and editing."""

import sys
import os
import re
import subprocess
import json
import threading
import time
import traceback
import faulthandler
from pathlib import Path

try:
    import av as _av
    import sounddevice as _sd
    _AUDIO_AVAILABLE = True
except ImportError:
    _AUDIO_AVAILABLE = False

# Fix Qt plugin conflict with OpenCV — import PyQt5 first
from PyQt5.QtCore import Qt, QTimer, QEvent
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QListWidgetItem, QSplitter,
    QMessageBox, QSizePolicy, QSlider, QStyle, QComboBox, QTextEdit, QFrame,
    QScrollArea, QCheckBox,
)
from PyQt5.QtGui import QFont, QPixmap, QImage, QMouseEvent

from services.text_extraction import (
    read_text_csv,
    write_text_csv,
    get_text_csv_path,
    VALID_TYPES,
)
from services.metadata import get_metadata

import cv2
import numpy as np
if "QT_QPA_PLATFORM_PLUGIN_PATH" in os.environ:
    del os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_sar(video_path: str) -> tuple[int, int]:
    """Return (sar_num, sar_den) via ffprobe. Falls back to (1, 1)."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=sample_aspect_ratio",
                "-of", "json",
                video_path,
            ],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout)
        sar_str = data["streams"][0].get("sample_aspect_ratio", "1:1")
        if sar_str in ("", "0:1", "1:1"):
            return (1, 1)
        parts = sar_str.replace("/", ":").split(":")
        return (int(parts[0]), int(parts[1]))
    except Exception:
        return (1, 1)


def _display_name(filename: str) -> str:
    """Return a clean display name (strips TMDb ID suffix)."""
    name = Path(filename).stem
    name = re.sub(r"\s*\{tmdb-\d+\}", "", name).strip()
    return name


def _frames_to_timecode(frame_number: int, fps: float) -> str:
    """Convert a frame number to HH:MM:SS.mmm string."""
    if fps <= 0:
        return "00:00:00.000"
    total_ms = int(round(frame_number / fps * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


# ---------------------------------------------------------------------------
# Audio player (background thread, PyAV + sounddevice)
# ---------------------------------------------------------------------------

class AudioPlayer:
    """Streams audio from a video file in a background thread."""

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None

    def play(self, video_path: str, start_secs: float):
        self.stop()
        if not _AUDIO_AVAILABLE:
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._stream,
            args=(str(video_path), start_secs, self._stop_event),
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread = None

    @staticmethod
    def _stream(video_path: str, start_secs: float, stop: threading.Event):
        container = None
        try:
            container = _av.open(video_path)
            audio_streams = [s for s in container.streams if s.type == "audio"]
            if not audio_streams:
                return
            audio_stream = audio_streams[0]
            sample_rate = audio_stream.codec_context.sample_rate
            channels = audio_stream.codec_context.channels or 2
            if start_secs > 0:
                container.seek(int(start_secs * 1_000_000))
            with _sd.OutputStream(samplerate=sample_rate, channels=channels, dtype="float32") as out:
                for frame in container.decode(audio_stream):
                    if stop.is_set():
                        break
                    pcm = np.ascontiguousarray(frame.to_ndarray().T)
                    out.write(pcm)
        except Exception:
            pass
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Click-to-seek slider
# ---------------------------------------------------------------------------

class ClickSeekSlider(QSlider):
    """QSlider that jumps to the exact position on single click."""
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
# List item for a text event
# ---------------------------------------------------------------------------

class TextEventItem(QListWidgetItem):
    """List item representing one on-screen text event."""

    def __init__(self, index: int, row: dict):
        super().__init__()
        self.index = index
        self.row = row
        self.update_display()

    def update_display(self):
        from PyQt5.QtGui import QColor
        start = self.row.get("start_frame", "?")
        end = self.row.get("end_frame", "?")
        type_ = self.row.get("type", "?")
        text = self.row.get("text", "")
        ignored = str(self.row.get("ignore", "")).strip().lower() in ("1", "true", "yes")
        try:
            score_val = float(self.row.get("score", 1.0))
            score_str = f"{score_val:.2f}"
        except (ValueError, TypeError):
            score_str = "   "
        snippet = text.replace("\n", " ")[:34]
        if len(text) > 34:
            snippet += "…"
        try:
            start_str = f"{int(start):07d}"
            end_str = f"{int(end):07d}"
        except (ValueError, TypeError):
            start_str = str(start)
            end_str = str(end)
        prefix = "✗ " if ignored else ""
        self.setText(f"{prefix}f{start_str}→f{end_str}  [{type_}] {score_str}  {snippet}")
        if ignored:
            self.setForeground(QColor("#888888"))  # dim ignored events
        else:
            self.setForeground(QColor("white"))


# ---------------------------------------------------------------------------
# Main validator window
# ---------------------------------------------------------------------------

class TextValidator(QMainWindow):
    """Frame-precise on-screen text event validator and editor."""

    def __init__(
        self,
        project_path: str,
        filenames: list,
        current_index: int = 0,
        media_type: str = "movies",
    ):
        super().__init__()
        self.project_path = project_path
        self.filenames = filenames
        self.current_movie_index = current_index
        self.filename = filenames[current_index]
        self.media_type = media_type
        self.rows: list[dict] = []
        self.current_event_index = 0
        self.modified = False
        self.cap = None
        self.is_playing = False
        self.current_frame_number = 0
        self.playback_timer = None
        self._updating_slider = False
        self._updating_combo = False
        self._updating_editor = False
        self._play_start_time = 0.0
        self._play_start_frame = 0
        self.show_quad = True
        self.audio = AudioPlayer()

        # Open video
        self.video_path = Path(project_path) / "media" / "videos" / media_type / self.filename
        if not self.video_path.exists():
            QMessageBox.critical(self, "Error", f"Video file not found:\n{self.video_path}")
            sys.exit(1)

        self.cap = cv2.VideoCapture(str(self.video_path))
        if not self.cap.isOpened():
            QMessageBox.critical(self, "Error", f"Could not open video:\n{self.video_path}")
            sys.exit(1)

        self.frame_rate = self.cap.get(cv2.CAP_PROP_FPS)
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        raw_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.video_native_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.sar_num, self.sar_den = _get_sar(str(self.video_path))
        self.video_native_width = int(round(raw_w * self.sar_num / self.sar_den))

        # Load text CSV
        try:
            self.rows = read_text_csv(project_path, self.filename, media_type)
            self._normalize_row_frames()
        except FileNotFoundError as e:
            QMessageBox.critical(self, "Error", str(e))
            sys.exit(1)

        # Playback timer
        self.playback_timer = QTimer()
        self.playback_timer.timeout.connect(self.advance_frame)
        if self.frame_rate > 0:
            self.playback_timer.setInterval(int(1000 / self.frame_rate))
        else:
            self.playback_timer.setInterval(42)

        self.setWindowTitle(
            f"Text Validator \u2014 {_display_name(self.filename)}  (1/{len(self.filenames)})"
        )

        self.init_ui()
        self.load_first_event()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def init_ui(self):
        main_widget = QWidget()
        main_widget.setStyleSheet("background-color: #808080; color: white;")
        self.setCentralWidget(main_widget)
        outer_layout = QVBoxLayout(main_widget)
        outer_layout.setContentsMargins(4, 4, 4, 4)
        outer_layout.setSpacing(4)

        # Movie selector row
        movie_row = QHBoxLayout()
        movie_label = QLabel("Movie:")
        movie_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        movie_row.addWidget(movie_label)
        self.movie_combo = QComboBox()
        self.movie_combo.setFocusPolicy(Qt.NoFocus)
        for fn in self.filenames:
            self.movie_combo.addItem(_display_name(fn), fn)
        self.movie_combo.setCurrentIndex(self.current_movie_index)
        self.movie_combo.currentIndexChanged.connect(self.on_movie_combo_changed)
        movie_row.addWidget(self.movie_combo, stretch=1)

        # Main splitter: left = video, right = sidebar
        splitter = QSplitter(Qt.Horizontal)
        outer_layout.addWidget(splitter, stretch=1)

        # ---- LEFT: video display ----
        frame_container = QWidget()
        frame_layout = QVBoxLayout(frame_container)
        frame_layout.setContentsMargins(0, 0, 0, 0)

        self.frame_label = QLabel()
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setScaledContents(False)
        self.frame_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.frame_label.setMinimumSize(1, 1)
        frame_layout.addWidget(self.frame_label, stretch=1)

        # Timeline scrub bar
        self.timeline_slider = ClickSeekSlider(Qt.Horizontal)
        self.timeline_slider.setMinimum(0)
        self.timeline_slider.setMaximum(max(0, self.total_frames - 1))
        self.timeline_slider.setValue(0)
        self.timeline_slider.setFocusPolicy(Qt.NoFocus)
        self.timeline_slider.valueChanged.connect(self.on_timeline_seek)
        self.timeline_slider.setToolTip(
            "Scrub timeline — click or drag to seek  [←/→ frame  Shift+←/→ 1 second]"
        )
        frame_layout.addWidget(self.timeline_slider)

        # ---- RIGHT: sidebar ----
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(2, 2, 2, 2)
        sidebar_layout.setSpacing(4)

        # Movie selector
        sidebar_layout.addLayout(movie_row)

        # Event list header
        list_header = QLabel("Text Events  (↑↓ navigate · click to jump)")
        list_header.setFont(QFont("Monospace", 8))
        sidebar_layout.addWidget(list_header)

        # Event list
        self.event_list = QListWidget()
        self.event_list.setFont(QFont("Monospace", 8))
        self.event_list.itemClicked.connect(self.on_event_selected)
        self.event_list.installEventFilter(self)
        self.event_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.event_list.setToolTip("Text events — click to jump  [↑↓ navigate]")
        sidebar_layout.addWidget(self.event_list, stretch=2)

        # Editor panel
        editor_frame = QFrame()
        editor_frame.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        editor_layout = QVBoxLayout(editor_frame)
        editor_layout.setContentsMargins(6, 6, 6, 6)
        editor_layout.setSpacing(4)

        # Type row
        type_row = QHBoxLayout()
        type_lbl = QLabel("Type:")
        type_lbl.setFixedWidth(44)
        type_row.addWidget(type_lbl)
        self.type_combo = QComboBox()
        self.type_combo.setEditable(True)
        for t in sorted(VALID_TYPES):
            self.type_combo.addItem(t)
        self.type_combo.setFocusPolicy(Qt.StrongFocus)
        self.type_combo.setToolTip("Editable — select a type or type a custom value")
        self.type_combo.currentTextChanged.connect(self.on_type_changed)
        type_row.addWidget(self.type_combo, stretch=1)
        editor_layout.addLayout(type_row)

        # Ignore checkbox
        ignore_row = QHBoxLayout()
        self.ignore_check = QCheckBox("Ignore this event  [I]")
        self.ignore_check.setFocusPolicy(Qt.NoFocus)
        self.ignore_check.setToolTip(
            "Mark event as ignored — dimmed in the list, excluded from exports  [I]"
        )
        self.ignore_check.toggled.connect(self.on_ignore_changed)
        ignore_row.addWidget(self.ignore_check)
        ignore_row.addStretch()
        editor_layout.addLayout(ignore_row)

        # Text edit
        text_lbl = QLabel("Text (editable):")
        text_lbl.setFont(QFont("Monospace", 8))
        editor_layout.addWidget(text_lbl)

        self.text_edit = QTextEdit()
        self.text_edit.setMinimumHeight(80)
        self.text_edit.setMaximumHeight(160)
        self.text_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.text_edit.setFont(QFont("Monospace", 9))
        self.text_edit.setToolTip("Edit the detected on-screen text  [Tab = back to list]")
        self.text_edit.setStyleSheet(
            "QTextEdit { background-color: #4a4a4a; color: white; border: 1px solid #888; }"
            "QTextEdit:focus { border: 2px solid #ff00ff; }"
        )
        self.text_edit.textChanged.connect(self.on_text_changed)
        self.text_edit.installEventFilter(self)
        editor_layout.addWidget(self.text_edit)

        self.info_label = QLabel()
        self.info_label.setFont(QFont("Monospace", 8))
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet(
            "background-color: #5a5a5a; padding: 4px; border: 1px solid #888;"
        )
        editor_layout.addWidget(self.info_label)

        # Remove button
        self.remove_button = QPushButton("⊗ Remove Event")
        self.remove_button.clicked.connect(self.remove_current_event)
        self.remove_button.setFocusPolicy(Qt.NoFocus)
        self.remove_button.setToolTip("Delete the selected text event from the CSV")
        self.remove_button.setStyleSheet(
            "QPushButton { background-color: #7a3030; border-color: #aa5555; }"
            "QPushButton:hover { background-color: #8a3a3a; }"
            "QPushButton:pressed { background-color: #5a2020; }"
        )
        editor_layout.addWidget(self.remove_button)

        sidebar_layout.addWidget(editor_frame, stretch=1)

        # Controls
        controls_layout = QHBoxLayout()

        self.play_pause_button = QPushButton("▶ Play")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.play_pause_button.setFocusPolicy(Qt.NoFocus)
        self.play_pause_button.setMinimumWidth(100)
        self.play_pause_button.setToolTip("Play / Pause  [Space]")
        controls_layout.addWidget(self.play_pause_button)

        self.save_button = QPushButton("💾 Save")
        self.save_button.clicked.connect(self.save_changes)
        self.save_button.setEnabled(False)
        self.save_button.setFocusPolicy(Qt.NoFocus)
        self.save_button.setToolTip("Save changes to CSV  [Ctrl+S]")
        controls_layout.addWidget(self.save_button)

        self.merge_button = QPushButton("⬅ Merge")
        self.merge_button.clicked.connect(self.merge_with_previous)
        self.merge_button.setFocusPolicy(Qt.NoFocus)
        self.merge_button.setToolTip("Merge current event into previous event  [M]")
        controls_layout.addWidget(self.merge_button)

        self.split_button = QPushButton("✂ Split")
        self.split_button.clicked.connect(self.split_at_current_frame)
        self.split_button.setFocusPolicy(Qt.NoFocus)
        self.split_button.setToolTip("Split current event at current frame  [N]")
        controls_layout.addWidget(self.split_button)

        self.quad_check = QCheckBox("Quad")
        self.quad_check.setChecked(True)
        self.quad_check.setFocusPolicy(Qt.NoFocus)
        self.quad_check.setToolTip("Show bounding quad overlay on frame  [Q]")
        self.quad_check.toggled.connect(self._on_quad_toggle)
        controls_layout.addWidget(self.quad_check)

        sidebar_layout.addLayout(controls_layout)

        # Keyboard hint
        hint = QLabel(
            "↑↓ event  Tab edit/list  Space play  Ctrl+S save  I ignore  M merge  N split  Q quad  ←→ frame  Shift+←→ 1s  Home/End movie"
        )
        hint.setFont(QFont("Monospace", 7))
        hint.setStyleSheet("color: #bbb;")
        hint.setWordWrap(True)
        sidebar_layout.addWidget(hint)

        splitter.addWidget(frame_container)
        splitter.addWidget(sidebar)
        sidebar.setMinimumWidth(360)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        self._populate_event_list()
        self.setFocus()

    # ------------------------------------------------------------------
    # Event list
    # ------------------------------------------------------------------

    def _populate_event_list(self):
        self.event_list.clear()
        for i, row in enumerate(self.rows):
            self.event_list.addItem(TextEventItem(i, row))

    def _refresh_event_item(self, index: int):
        item = self.event_list.item(index)
        if isinstance(item, TextEventItem):
            item.update_display()

    # ------------------------------------------------------------------
    # Frame utilities (shared with shot_validator pattern)
    # ------------------------------------------------------------------

    def _normalize_row_frames(self):
        for row in self.rows:
            for key in ("start_frame", "end_frame"):
                val = row.get(key)
                if isinstance(val, str):
                    try:
                        row[key] = int(val)
                    except ValueError:
                        row[key] = 0

    def get_frame(self, frame_number: int) -> np.ndarray | None:
        if frame_number < 0 or frame_number >= self.total_frames:
            return None
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        ret, frame = self.cap.read()
        return frame if ret else None

    def _current_quad_pts(self) -> "np.ndarray | None":
        """Return the SAR-corrected quad polygon for the current event, or None.

        Quad coords in the CSV are in raw (pre-SAR) video pixel space.
        Returns an (4, 1, 2) int32 array suitable for cv2.polylines, or None.
        """
        if not (0 <= self.current_event_index < len(self.rows)):
            return None
        row = self.rows[self.current_event_index]
        sf = int(row.get("start_frame", 0))
        ef = int(row.get("end_frame", 0))
        if not (sf <= self.current_frame_number <= ef):
            return None
        quad_str = row.get("quad", "")
        if not quad_str:
            return None
        try:
            parts = [int(v) for v in quad_str.split(",") if v.strip()]
        except ValueError:
            return None
        if len(parts) != 8:
            return None
        pts = np.array(parts, dtype=np.float64).reshape(4, 2)
        if (self.sar_num, self.sar_den) != (1, 1):
            pts[:, 0] *= self.sar_num / self.sar_den
        return pts.astype(np.int32).reshape(4, 1, 2)

    def display_frame(self, frame: np.ndarray):
        if frame is None:
            self.frame_label.setText("Failed to load frame")
            return
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        if (self.sar_num, self.sar_den) != (1, 1):
            display_w = int(round(frame_rgb.shape[1] * self.sar_num / self.sar_den))
            frame_rgb = cv2.resize(
                frame_rgb, (display_w, frame_rgb.shape[0]),
                interpolation=cv2.INTER_LINEAR,
            )
        if self.show_quad:
            quad_pts = self._current_quad_pts()
            if quad_pts is not None:
                cv2.polylines(frame_rgb, [quad_pts], isClosed=True, color=(0, 255, 0), thickness=2)
        height, width, channel = frame_rgb.shape
        bytes_per_line = 3 * width
        q_image = QImage(
            frame_rgb.data, width, height, bytes_per_line, QImage.Format_RGB888
        )
        pixmap = QPixmap.fromImage(q_image)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                self.frame_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self.frame_label.setPixmap(scaled)

    def update_timeline_slider(self):
        self._updating_slider = True
        self.timeline_slider.setValue(self.current_frame_number)
        self._updating_slider = False

    def on_timeline_seek(self, value: int):
        if self._updating_slider:
            return
        if self.is_playing:
            self.stop_playback()
        self.current_frame_number = value
        frame = self.get_frame(self.current_frame_number)
        if frame is not None:
            self.display_frame(frame)
        self._update_frame_info_label()

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def toggle_play_pause(self):
        if self.is_playing:
            self.stop_playback()
        else:
            self.start_playback()

    def start_playback(self):
        self.is_playing = True
        self.play_pause_button.setText("⏸ Pause")
        self._play_start_frame = self.current_frame_number
        start_secs = (
            self.current_frame_number / self.frame_rate if self.frame_rate > 0 else 0.0
        )
        self.audio.play(str(self.video_path), start_secs)
        QTimer.singleShot(130, self._begin_video_timer)

    def _begin_video_timer(self):
        try:
            if not self.is_playing:
                return
            self._play_start_time = time.perf_counter()
            self.playback_timer.start()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.stop_playback()

    def stop_playback(self):
        self.is_playing = False
        self.play_pause_button.setText("▶ Play")
        self.playback_timer.stop()
        self.audio.stop()

    def advance_frame(self):
        try:
            self._advance_frame_inner()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.stop_playback()

    def _advance_frame_inner(self):
        if not self.is_playing:
            return
        elapsed = time.perf_counter() - self._play_start_time
        target_frame = self._play_start_frame + int(elapsed * self.frame_rate)
        target_frame = min(target_frame, self.total_frames - 1)
        if target_frame <= self.current_frame_number:
            return
        self.current_frame_number = target_frame
        if self.current_frame_number >= self.total_frames:
            self.stop_playback()
            return
        self.update_timeline_slider()
        frame = self.get_frame(self.current_frame_number)
        if frame is not None:
            self.display_frame(frame)
        self._update_frame_info_label()

    # ------------------------------------------------------------------
    # Frame stepping
    # ------------------------------------------------------------------

    def next_frame(self):
        if self.is_playing:
            self.stop_playback()
        nxt = self.current_frame_number + 1
        if nxt < self.total_frames:
            self.current_frame_number = nxt
            self.update_timeline_slider()
            frame = self.get_frame(self.current_frame_number)
            if frame is not None:
                self.display_frame(frame)
            self._update_frame_info_label()

    def prev_frame(self):
        if self.is_playing:
            self.stop_playback()
        prv = self.current_frame_number - 1
        if prv >= 0:
            self.current_frame_number = prv
            self.update_timeline_slider()
            frame = self.get_frame(self.current_frame_number)
            if frame is not None:
                self.display_frame(frame)
            self._update_frame_info_label()

    def step_seconds(self, direction: int):
        if self.is_playing:
            self.stop_playback()
        frames = max(1, int(round(self.frame_rate)))
        target = max(
            0, min(self.total_frames - 1, self.current_frame_number + frames * direction)
        )
        self.current_frame_number = target
        self.update_timeline_slider()
        frame = self.get_frame(self.current_frame_number)
        if frame is not None:
            self.display_frame(frame)
        self._update_frame_info_label()

    def _update_frame_info_label(self):
        self._sync_list_highlight()
        if 0 <= self.current_event_index < len(self.rows):
            self._update_readonly_info(self.rows[self.current_event_index])
        else:
            self.info_label.setText(f"current_frame: {self.current_frame_number}")

    def _sync_list_highlight(self):
        """Select/deselect the list item based on whether the current frame is inside any event's range."""
        cf = self.current_frame_number
        # Check if the currently tracked event still covers this frame
        idx = self.current_event_index
        if 0 <= idx < len(self.rows):
            row = self.rows[idx]
            if int(row.get("start_frame", 0)) <= cf <= int(row.get("end_frame", 0)):
                # Still inside the same event — ensure it's highlighted
                if self.event_list.currentRow() != idx:
                    self.event_list.setCurrentRow(idx)
                return

        # Search all events for one that contains this frame
        for i, row in enumerate(self.rows):
            if int(row.get("start_frame", 0)) <= cf <= int(row.get("end_frame", 0)):
                self.current_event_index = i
                self.event_list.setCurrentRow(i)
                self._load_event_into_editor(row)
                self._update_readonly_info(row)
                return

        # Frame is between events — clear selection
        self.event_list.clearSelection()

    # ------------------------------------------------------------------
    # Event navigation
    # ------------------------------------------------------------------

    def load_first_event(self):
        if self.rows:
            self.jump_to_event(0)
        else:
            frame = self.get_frame(0)
            if frame is not None:
                self.display_frame(frame)
            self.info_label.setText("(no text events in this CSV)")
            self._update_frame_info_label()

    def next_event(self):
        if self.current_event_index < len(self.rows) - 1:
            self.jump_to_event(self.current_event_index + 1)

    def prev_event(self):
        if self.current_event_index > 0:
            self.jump_to_event(self.current_event_index - 1)

    def jump_to_event(self, index: int):
        was_playing = self.is_playing
        if self.is_playing:
            self.stop_playback()

        if not (0 <= index < len(self.rows)):
            return

        self.current_event_index = index
        row = self.rows[index]
        frame_number = int(row.get("start_frame", 0))

        self.current_frame_number = frame_number
        self.update_timeline_slider()

        frame = self.get_frame(frame_number)
        if frame is not None:
            self.display_frame(frame)

        self.event_list.setCurrentRow(index)
        self._load_event_into_editor(row)
        self._update_readonly_info(row)
        self._update_frame_info_label()

        if was_playing:
            self.start_playback()

    def on_event_selected(self, item: TextEventItem):
        self.jump_to_event(item.index)

    # ------------------------------------------------------------------
    # Editor
    # ------------------------------------------------------------------

    def _load_event_into_editor(self, row: dict):
        """Populate type combo, ignore checkbox, and text edit from row."""
        self._updating_editor = True
        try:
            type_val = row.get("type", "")
            idx = self.type_combo.findText(type_val, Qt.MatchFixedString)
            if idx >= 0:
                self.type_combo.setCurrentIndex(idx)
            else:
                self.type_combo.setEditText(type_val)

            ignored = str(row.get("ignore", "")).strip().lower() in ("1", "true", "yes")
            self.ignore_check.setChecked(ignored)

            self.text_edit.blockSignals(True)
            self.text_edit.setPlainText(row.get("text", ""))
            self.text_edit.blockSignals(False)
        finally:
            self._updating_editor = False
        self.remove_button.setEnabled(bool(row))

    def _update_readonly_info(self, row: dict):
        start_tc = row.get("start_time", "?")
        end_tc = row.get("end_time", "?")
        start_f = row.get("start_frame", "?")
        end_f = row.get("end_frame", "?")
        lang = row.get("language", "?")
        fn = Path(row.get("filename", "")).name or self.filename
        n = len(self.rows)
        score_str = row.get("score", "")
        try:
            score_display = f"{float(score_str):.3f}" if score_str != "" else "—"
        except (ValueError, TypeError):
            score_display = score_str or "—"
        ignored = str(row.get("ignore", "")).strip().lower() in ("1", "true", "yes")
        self.info_label.setText(
            f"current_frame: {self.current_frame_number}\n"
            f"filename:    {fn}\n"
            f"language:    {lang}\n"
            f"score:       {score_display}\n"
            f"ignore:      {'yes' if ignored else 'no'}\n"
            f"start_frame: {start_f}\n"
            f"end_frame:   {end_f}\n"
            f"start_time:  {start_tc}\n"
            f"end_time:    {end_tc}\n"
            f"event {self.current_event_index + 1} / {n}"
        )

    def on_ignore_changed(self, checked: bool):
        if self._updating_editor:
            return
        if 0 <= self.current_event_index < len(self.rows):
            self.rows[self.current_event_index]["ignore"] = "1" if checked else ""
            self._refresh_event_item(self.current_event_index)
            self._mark_modified()

    def on_type_changed(self, text: str):
        if self._updating_editor:
            return
        if 0 <= self.current_event_index < len(self.rows):
            self.rows[self.current_event_index]["type"] = text
            self._refresh_event_item(self.current_event_index)
            self._mark_modified()

    def on_text_changed(self):
        if self._updating_editor:
            return
        if 0 <= self.current_event_index < len(self.rows):
            self.rows[self.current_event_index]["text"] = self.text_edit.toPlainText()
            self._refresh_event_item(self.current_event_index)
            self._mark_modified()

    def _on_quad_toggle(self, checked: bool):
        self.show_quad = checked
        frame = self.get_frame(self.current_frame_number)
        if frame is not None:
            self.display_frame(frame)

    def _mark_modified(self):
        if not self.modified:
            self.modified = True
            self.save_button.setEnabled(True)

    def remove_current_event(self):
        """Delete the currently selected text event from the in-memory list."""
        if not self.rows:
            return
        index = self.current_event_index
        if not (0 <= index < len(self.rows)):
            return

        self.rows.pop(index)
        self._mark_modified()

        # Rebuild list; land on the same position (or last item if we removed the last)
        self._populate_event_list()

        if not self.rows:
            self.current_event_index = 0
            self.info_label.setText("(no text events in this CSV)")
            self._load_event_into_editor({})
            return

        new_index = min(index, len(self.rows) - 1)
        self.jump_to_event(new_index)

    def merge_with_previous(self):
        """Merge the current event into the previous one (extend previous end to current end)."""
        if self.current_event_index <= 0:
            QMessageBox.warning(self, "Cannot Merge", "Cannot merge the first event — there is no previous event.")
            return

        cur = self.rows[self.current_event_index]
        prev = self.rows[self.current_event_index - 1]

        # Extend previous event to cover this one's range
        prev["end_frame"] = cur["end_frame"]
        prev["end_time"] = cur["end_time"]
        # Concatenate text with a newline separator
        prev_text = prev.get("text", "").strip()
        cur_text = cur.get("text", "").strip()
        prev["text"] = (prev_text + "\n" + cur_text).strip()

        self.rows.pop(self.current_event_index)
        self._populate_event_list()

        self.current_event_index -= 1
        self.jump_to_event(self.current_event_index)
        self._mark_modified()

    def split_at_current_frame(self):
        """Split the current event at the current frame position into two events."""
        if not (0 <= self.current_event_index < len(self.rows)):
            return

        row = self.rows[self.current_event_index]
        start_frame = int(row.get("start_frame", 0))
        end_frame = int(row.get("end_frame", 0))
        split_frame = self.current_frame_number

        if split_frame <= start_frame:
            QMessageBox.warning(
                self, "Cannot Split",
                "Cannot split at the start frame — step forward at least one frame.",
            )
            return
        if split_frame > end_frame:
            QMessageBox.warning(
                self, "Cannot Split",
                "Current frame is beyond the end of this event.",
            )
            return

        first = dict(row)
        first["end_frame"] = split_frame - 1
        first["end_time"] = _frames_to_timecode(split_frame - 1, self.frame_rate)

        second = dict(row)
        second["start_frame"] = split_frame
        second["start_time"] = _frames_to_timecode(split_frame, self.frame_rate)
        second["text"] = ""

        self.rows[self.current_event_index:self.current_event_index + 1] = [first, second]
        self._populate_event_list()

        # Land on the new second half
        self.jump_to_event(self.current_event_index + 1)
        self._mark_modified()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_changes(self):
        try:
            write_text_csv(
                self.project_path,
                self.filename,
                self.rows,
                self.media_type,
                force=True,
            )
            self.modified = False
            self.save_button.setEnabled(False)
            QMessageBox.information(self, "Saved", "Text CSV saved successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save text CSV:\n{e}")

    # ------------------------------------------------------------------
    # Movie switching
    # ------------------------------------------------------------------

    def on_movie_combo_changed(self, index: int):
        if self._updating_combo:
            return
        self.switch_to_movie(index)

    def switch_to_movie(self, index: int):
        if index == self.current_movie_index:
            return

        if self.modified:
            reply = QMessageBox.question(
                self,
                "Unsaved Changes",
                "You have unsaved changes. Save before switching?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                self._updating_combo = True
                self.movie_combo.setCurrentIndex(self.current_movie_index)
                self._updating_combo = False
                return
            elif reply == QMessageBox.Save:
                self.save_changes()

        was_playing = self.is_playing
        if self.is_playing:
            self.stop_playback()

        if self.cap is not None:
            self.cap.release()

        self.current_movie_index = index
        self.filename = self.filenames[index]
        self.rows = []
        self.current_event_index = 0
        self.modified = False
        self.current_frame_number = 0

        self.video_path = (
            Path(self.project_path) / "media" / "videos" / self.media_type / self.filename
        )
        if not self.video_path.exists():
            QMessageBox.critical(self, "Error", f"Video file not found:\n{self.video_path}")
            return

        self.cap = cv2.VideoCapture(str(self.video_path))
        if not self.cap.isOpened():
            QMessageBox.critical(self, "Error", f"Could not open video:\n{self.video_path}")
            return

        self.frame_rate = self.cap.get(cv2.CAP_PROP_FPS)
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        raw_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.video_native_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.sar_num, self.sar_den = _get_sar(str(self.video_path))
        self.video_native_width = int(round(raw_w * self.sar_num / self.sar_den))
        if self.frame_rate > 0:
            self.playback_timer.setInterval(int(1000 / self.frame_rate))

        try:
            self.rows = read_text_csv(self.project_path, self.filename, self.media_type)
            self._normalize_row_frames()
        except FileNotFoundError as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.timeline_slider.setMaximum(max(0, self.total_frames - 1))
        self.timeline_slider.setValue(0)
        self.save_button.setEnabled(False)
        self.setWindowTitle(
            f"Text Validator \u2014 {_display_name(self.filename)}"
            f"  ({self.current_movie_index + 1}/{len(self.filenames)})"
        )

        self._updating_combo = True
        self.movie_combo.setCurrentIndex(index)
        self._updating_combo = False

        self._populate_event_list()
        self.load_first_event()

        if was_playing:
            self.start_playback()

    # ------------------------------------------------------------------
    # Keyboard shortcuts and event filter
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.KeyPress:
                key = event.key()
                mods = event.modifiers()
                if obj == self.event_list:
                    # Tab from list → focus text editor
                    if key == Qt.Key_Tab and not (mods & Qt.ShiftModifier):
                        self.text_edit.setFocus()
                        self.text_edit.moveCursor(self.text_edit.textCursor().End)
                        return True
                    if key in (
                        Qt.Key_Space,
                        Qt.Key_Left, Qt.Key_Right,
                        Qt.Key_Up, Qt.Key_Down,
                        Qt.Key_Home, Qt.Key_End,
                    ):
                        self.keyPressEvent(event)
                        return True
                elif obj == self.text_edit:
                    # Tab from text editor → focus list
                    if key == Qt.Key_Tab and not (mods & Qt.ShiftModifier):
                        self.event_list.setFocus()
                        return True
                    # Shift+Tab also returns to list
                    if key == Qt.Key_Tab and (mods & Qt.ShiftModifier):
                        self.event_list.setFocus()
                        return True
                    # Let Ctrl+S still work while editing
                    if key == Qt.Key_S and (mods & Qt.ControlModifier):
                        if self.modified:
                            self.save_changes()
                        return True
            return super().eventFilter(obj, event)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        if key == Qt.Key_Space:
            self.toggle_play_pause()
        elif key == Qt.Key_Down:
            self.next_event()
        elif key == Qt.Key_Up:
            self.prev_event()
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
        elif key == Qt.Key_S and mods & Qt.ControlModifier:
            if self.modified:
                self.save_changes()
        elif key == Qt.Key_I:
            self.ignore_check.setChecked(not self.ignore_check.isChecked())
        elif key == Qt.Key_M:
            self.merge_with_previous()
        elif key == Qt.Key_N:
            self.split_at_current_frame()
        elif key == Qt.Key_Q:
            self.quad_check.setChecked(not self.quad_check.isChecked())
        elif key == Qt.Key_Home:
            if self.current_movie_index > 0:
                self.switch_to_movie(self.current_movie_index - 1)
        elif key == Qt.Key_End:
            if self.current_movie_index < len(self.filenames) - 1:
                self.switch_to_movie(self.current_movie_index + 1)
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        if self.is_playing:
            self.stop_playback()
        if self.cap is not None:
            self.cap.release()

        if self.modified:
            reply = QMessageBox.question(
                self,
                "Unsaved Changes",
                "You have unsaved changes. Save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
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


# ---------------------------------------------------------------------------
# Entry point (invoked as a subprocess by cli.py)
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate and edit on-screen text events with frame-precise display"
    )
    parser.add_argument("query", nargs="?", help="Filename substring to match")
    parser.add_argument("--tmdb", type=int, help="TMDb ID")
    parser.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    parser.add_argument("--project", help="Project path")
    parser.add_argument(
        "--filenames", nargs="+",
        help="Explicit list of video filenames (passed by cli.py)",
    )
    parser.add_argument("--all", action="store_true", help="Validate all films with text CSVs")

    args = parser.parse_args()

    # Resolve project path
    if args.project:
        project_path = args.project
    else:
        cwd = Path.cwd()
        if (cwd / "media").exists():
            project_path = str(cwd)
        elif (cwd.parent / "media").exists():
            project_path = str(cwd.parent)
        else:
            print(
                "✗ Could not find project path. Use --project or run from project directory.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Resolve filenames
    if args.filenames:
        filenames = args.filenames
    elif getattr(args, "all", False):
        entries = get_metadata(project_path, media_type=args.media)
        filenames = [
            e["filename"]
            for e in entries
            if e.get("filename")
            and get_text_csv_path(project_path, e["filename"], args.media).exists()
        ]
        if not filenames:
            print("✗ No text CSVs found.", file=sys.stderr)
            sys.exit(1)
    elif args.tmdb:
        entries = get_metadata(project_path, media_type=args.media)
        filenames = [e["filename"] for e in entries if e.get("tmdb") == str(args.tmdb)]
        if not filenames:
            print(f"✗ No file found with TMDb ID: {args.tmdb}", file=sys.stderr)
            sys.exit(1)
    elif args.query:
        entries = get_metadata(project_path, query=args.query, media_type=args.media)
        if not entries:
            print(f"✗ No file found matching '{args.query}'", file=sys.stderr)
            sys.exit(1)
        elif len(entries) > 1:
            print(f"✗ Multiple files match '{args.query}':", file=sys.stderr)
            for e in entries:
                print(f"  - {e['filename']}", file=sys.stderr)
            print("Use --tmdb <id> or a more specific query.", file=sys.stderr)
            sys.exit(1)
        filenames = [entries[0]["filename"]]
    else:
        print("✗ Must provide a query, --tmdb, --filenames, or --all", file=sys.stderr)
        sys.exit(1)

    # Verify text CSVs exist
    for fn in filenames:
        csv_path = get_text_csv_path(project_path, fn, args.media)
        if not csv_path.exists():
            print(f"✗ No text CSV found for {fn}", file=sys.stderr)
            print("Run 'crossing text extract' first to generate the text CSV.", file=sys.stderr)
            sys.exit(1)

    faulthandler.enable()

    app = QApplication(sys.argv)
    app.setStyleSheet(
        """
        QWidget          { background-color: #808080; color: white; }
        QPushButton      { background-color: #666; color: white; border: 1px solid #999; padding: 3px 8px; border-radius: 3px; }
        QPushButton:hover      { background-color: #777; }
        QPushButton:pressed    { background-color: #555; }
        QPushButton:checked    { background-color: #ff00ff; border-color: #ff66ff; }
        QPushButton:disabled   { color: #aaa; border-color: #777; }
        QComboBox        { background-color: #666; color: white; border: 1px solid #999; padding: 2px 6px; }
        QComboBox QAbstractItemView { background-color: #666; color: white; selection-background-color: #ff00ff; }
        QListWidget      { background-color: #5a5a5a; color: white; border: 1px solid #888; }
        QListWidget::item:selected { background-color: #ff00ff; color: white; }
        QListWidget::item:hover    { background-color: #6a6a6a; }
        QSlider::groove:horizontal { background: #555; height: 6px; border-radius: 3px; }
        QSlider::handle:horizontal { background: #ccc; width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; }
        QSlider::sub-page:horizontal { background: #ff00ff; border-radius: 3px; }
        QScrollBar:vertical        { background: #666; width: 10px; }
        QScrollBar::handle:vertical { background: #999; border-radius: 4px; min-height: 20px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QLabel           { background-color: transparent; color: white; }
        QSplitter::handle { background-color: #666; }
        QMessageBox      { background-color: #808080; color: white; }
        QTextEdit        { background-color: #5a5a5a; color: white; border: 1px solid #888; }
        """
    )

    validator = TextValidator(project_path, filenames, 0, args.media)

    screen = QApplication.primaryScreen()
    avail = screen.availableGeometry()
    validator.setGeometry(avail)
    validator.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
