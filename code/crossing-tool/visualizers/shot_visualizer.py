#!/usr/bin/env python3
"""Shot Visualizer - Frame-precise shot boundary review."""

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
from PyQt5.QtCore import Qt, QTimer, QEvent
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QListWidgetItem, QSplitter,
    QMessageBox, QSizePolicy, QSlider, QStyle, QComboBox
)
from PyQt5.QtGui import QFont, QPixmap, QImage, QMouseEvent

from data.shotlist import read_shotlist, write_shotlist, get_shotlist_path
from data.metadata import get_metadata
from data.annotate import reindex_annotations_for_merge, reindex_annotations_for_split


def _get_sar(video_path: str) -> tuple[int, int]:
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

# Import cv2 after PyQt5 and fix plugin path conflict
import cv2
import numpy as np
if 'QT_QPA_PLATFORM_PLUGIN_PATH' in os.environ:
    del os.environ['QT_QPA_PLATFORM_PLUGIN_PATH']


def frames_to_timecode(frame_number: int, fps: float) -> str:
    """Convert a frame number to HH:MM:SS.mmm timecode string."""
    total_ms = int(round(frame_number / fps * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


class AudioPlayer:
    """Streams audio from a video file in a background thread (PyAV + sounddevice)."""

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None

    def play(self, video_path: str, start_secs: float):
        """Start audio playback from start_secs. Stops any current playback first."""
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
        """Signal audio to stop. Non-blocking — daemon thread finishes on its own."""
        self._stop_event.set()
        self._thread = None

    @staticmethod
    def _stream(video_path: str, start_secs: float, stop: threading.Event):
        container = None
        try:
            container = _av.open(video_path)
            audio_streams = [s for s in container.streams if s.type == 'audio']
            if not audio_streams:
                return
            audio_stream = audio_streams[0]
            sample_rate = audio_stream.codec_context.sample_rate
            channels = audio_stream.codec_context.channels or 2
            if start_secs > 0:
                # seek uses microseconds in container time base
                container.seek(int(start_secs * 1_000_000))
            with _sd.OutputStream(samplerate=sample_rate, channels=channels, dtype='float32') as out:
                for frame in container.decode(audio_stream):
                    if stop.is_set():
                        break
                    # fltp = planar float: shape (channels, samples) — transpose to (samples, channels)
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


class ShotItem(QListWidgetItem):
    """List item representing a shot."""
    def __init__(self, index: int, shot: dict):
        super().__init__()
        self.index = index
        self.shot = shot
        self.is_ignored = shot.get('Ignore', 'No') == 'Yes'
        self.update_display()
    
    def update_display(self):
        """Update the display text."""
        # pad scene and shot with values for proper sorting (up to 9999 scenes/shots)
        scene_value = f"{int(self.shot.get('Scene', 0)):04d}"
        shot_value = f"{self.index:04d}"
        start_frame = self.shot.get('start_frame', '?')
        end_frame = self.shot.get('end_frame', '?')
        # confidence = self.shot.get('Shot_Confidence', '')
        ignored = " [X]" if self.is_ignored else ""
        # conf_str = f" ({confidence})" if confidence else ""
        # if ignored show (x) at end of line
        self.setText(f"{scene_value} | {shot_value} | f{start_frame:07d} → f{end_frame:07d} | {ignored}")

    def toggle_ignore(self):
        """Toggle ignore status."""
        self.is_ignored = not self.is_ignored
        self.shot['Ignore'] = 'Yes' if self.is_ignored else 'No'
        self.update_display()


class OpenCVShotVisualizer(QMainWindow):
    """Frame-precise shot visualizer."""

    def __init__(self, project_path: str, filenames: list, current_index: int = 0, media_type: str = "movies"):
        super().__init__()
        self.project_path = project_path
        self.filenames = filenames
        self.current_movie_index = current_index
        self.filename = filenames[current_index]
        self.media_type = media_type
        self.shots = []
        self.current_shot_index = 0
        self.modified = False
        self.cap = None
        self.is_playing = False
        self.continue_playback = True  # Continue past shot boundaries during playback
        self.current_frame_number = 0
        self.playback_timer = None
        self._updating_slider = False
        self._updating_combo = False
        self._play_start_time = 0.0
        self._play_start_frame = 0
        self.audio = AudioPlayer()
        self.gremlins_active = False
        self.gremlins_timer = QTimer()
        self.gremlins_timer.setInterval(5000)
        self.gremlins_timer.timeout.connect(self.gremlin_tick)

        # Set up video path
        self.video_path = Path(project_path) / "media" / "videos" / media_type / self.filename
        if not self.video_path.exists():
            QMessageBox.critical(self, "Error", f"Video file not found:\n{self.video_path}")
            sys.exit(1)
        
        # Open video with OpenCV
        self.cap = cv2.VideoCapture(str(self.video_path))
        if not self.cap.isOpened():
            QMessageBox.critical(self, "Error", f"Could not open video:\n{self.video_path}")
            sys.exit(1)
        
        # Get video properties
        self.frame_rate = self.cap.get(cv2.CAP_PROP_FPS)
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        raw_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.video_native_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.sar_num, self.sar_den = _get_sar(str(self.video_path))
        # Display width accounts for non-square pixels
        self.video_native_width = int(round(raw_w * self.sar_num / self.sar_den))
        
        # Load shotlist
        try:
            self.shots = read_shotlist(project_path, self.filename, media_type)
            # Convert string frame numbers to int if needed
            for shot in self.shots:
                if 'start_frame' in shot and isinstance(shot['start_frame'], str):
                    shot['start_frame'] = int(shot['start_frame'])
                if 'end_frame' in shot and isinstance(shot['end_frame'], str):
                    shot['end_frame'] = int(shot['end_frame'])
        except FileNotFoundError as e:
            QMessageBox.critical(self, "Error", str(e))
            sys.exit(1)
        
        # Set up playback timer
        self.playback_timer = QTimer()
        self.playback_timer.timeout.connect(self.advance_frame)
        # Calculate interval for ~real-time playback (1000ms / fps)
        if self.frame_rate > 0:
            self.playback_timer.setInterval(int(1000 / self.frame_rate))
        else:
            self.playback_timer.setInterval(42)  # ~24fps fallback
        
        self.setWindowTitle(f"Shotlist Visualizer \u2014 {_display_name(self.filename)}  (1/{len(self.filenames)})")
        
        self.init_ui()
        self.load_first_shot()
    
    def init_ui(self):
        """Initialize the user interface."""
        from PyQt5.QtWidgets import QGridLayout

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        outer_layout = QVBoxLayout(main_widget)
        outer_layout.setContentsMargins(4, 4, 4, 4)
        outer_layout.setSpacing(4)

        splitter = QSplitter(Qt.Horizontal)
        outer_layout.addWidget(splitter, stretch=1)

        # ---- LEFT: video + timeline ----
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
        self.timeline_slider.valueChanged.connect(self.on_timeline_seek)
        self.timeline_slider.setToolTip("Scrub timeline — click or drag to seek  [←/→ frame  Shift+←/→ 1 second]")
        left_layout.addWidget(self.timeline_slider)

        # ---- RIGHT: controls panel ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(2, 2, 2, 2)
        right_layout.setSpacing(4)

        # Movie selector
        movie_row = QHBoxLayout()
        self.movie_combo = QComboBox()
        self.movie_combo.setFocusPolicy(Qt.NoFocus)
        for fn in self.filenames:
            self.movie_combo.addItem(_display_name(fn), fn)
        self.movie_combo.setCurrentIndex(self.current_movie_index)
        self.movie_combo.currentIndexChanged.connect(self.on_movie_combo_changed)
        movie_row.addWidget(self.movie_combo, stretch=1)
        right_layout.addLayout(movie_row)

        # Scene and shot lists side by side
        lists_container = QWidget()
        lists_layout = QHBoxLayout(lists_container)
        lists_layout.setContentsMargins(0, 0, 0, 0)
        lists_layout.setSpacing(4)

        # Scene list (narrow — just scene index numbers)
        scene_col = QWidget()
        scene_col_layout = QVBoxLayout(scene_col)
        scene_col_layout.setContentsMargins(0, 0, 0, 0)
        scene_col_layout.setSpacing(2)
        scene_header = QLabel("Scene")
        scene_header.setAlignment(Qt.AlignCenter)
        scene_col_layout.addWidget(scene_header)
        self.scene_list = QListWidget()
        self.scene_list.setMaximumWidth(65)
        self.scene_list.setMinimumWidth(50)
        self.scene_list.setFocusPolicy(Qt.NoFocus)
        self.scene_list.itemClicked.connect(self.on_scene_selected)
        self.scene_list.setToolTip("Scenes — click to jump  [PgUp/PgDn navigate  ✂/⬅ split/merge]")
        scene_col_layout.addWidget(self.scene_list)
        lists_layout.addWidget(scene_col)

        # Shot list
        shot_col = QWidget()
        shot_col_layout = QVBoxLayout(shot_col)
        shot_col_layout.setContentsMargins(0, 0, 0, 0)
        shot_col_layout.setSpacing(2)
        shot_header = QLabel("Scene | Shot | Frames     | Ignored")
        shot_col_layout.addWidget(shot_header)
        self.shot_list = QListWidget()
        self.shot_list.itemClicked.connect(self.on_shot_selected)
        self.shot_list.installEventFilter(self)
        self.shot_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.shot_list.setToolTip("Shots — click to jump  [↑/↓ navigate]")
        for i, shot in enumerate(self.shots):
            self.shot_list.addItem(ShotItem(i, shot))
        shot_col_layout.addWidget(self.shot_list)
        lists_layout.addWidget(shot_col, stretch=1)

        right_layout.addWidget(lists_container, stretch=1)

        # Stats
        self.stats_label = QLabel()
        self.stats_label.setWordWrap(True)
        self.update_stats()
        right_layout.addWidget(self.stats_label, stretch=0)

        # Frame info
        self.info_label = QLabel()
        self.info_label.setFont(theme.font_mono())
        self.info_label.setAlignment(Qt.AlignLeft)
        self.info_label.setWordWrap(True)
        self.info_label.setMinimumHeight(80)
        right_layout.addWidget(self.info_label, stretch=0)

        # Buttons in a grid — 3 rows
        btn_grid = QGridLayout()
        btn_grid.setSpacing(4)

        # Row 0: shot editing
        self.split_button = QPushButton("New Shot")
        self.split_button.clicked.connect(self.split_shot_at_current_frame)
        self.split_button.setFocusPolicy(Qt.NoFocus)
        self.split_button.setToolTip("Split current shot at current frame  [Shift+N]")
        btn_grid.addWidget(self.split_button, 0, 0)

        self.merge_button = QPushButton("Merge Shot")
        self.merge_button.clicked.connect(self.merge_with_previous)
        self.merge_button.setFocusPolicy(Qt.NoFocus)
        self.merge_button.setToolTip("Merge current shot with previous shot  [Shift+M]")
        btn_grid.addWidget(self.merge_button, 0, 1)

        self.save_button = QPushButton("💾 Save")
        self.save_button.clicked.connect(self.save_changes)
        self.save_button.setEnabled(False)
        self.save_button.setFocusPolicy(Qt.NoFocus)
        self.save_button.setToolTip("Save changes to CSV  [Ctrl+S]")
        btn_grid.addWidget(self.save_button, 0, 2)

        # Row 1: scene editing
        self.split_scene_button = QPushButton("New Scene")
        self.split_scene_button.clicked.connect(self.split_scene_at_current_shot)
        self.split_scene_button.setFocusPolicy(Qt.NoFocus)
        self.split_scene_button.setToolTip("Start a new scene at current shot  [N]")
        btn_grid.addWidget(self.split_scene_button, 1, 0)

        self.merge_scene_button = QPushButton("Merge Scene")
        self.merge_scene_button.clicked.connect(self.merge_scene_at_current_shot)
        self.merge_scene_button.setFocusPolicy(Qt.NoFocus)
        self.merge_scene_button.setToolTip("Merge current scene into previous scene  [M]")
        btn_grid.addWidget(self.merge_scene_button, 1, 1)

        self.ignore_button = QPushButton("⊗ Ignore")
        self.ignore_button.clicked.connect(self.toggle_current_ignore)
        self.ignore_button.setFocusPolicy(Qt.NoFocus)
        self.ignore_button.setToolTip("Toggle Ignore on current shot  [I]")
        btn_grid.addWidget(self.ignore_button, 1, 2)

        # Row 2: playback
        self.play_pause_button = QPushButton("▶ Play")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.play_pause_button.setFocusPolicy(Qt.NoFocus)
        self.play_pause_button.setToolTip("Play / Pause  [Space]")
        btn_grid.addWidget(self.play_pause_button, 2, 0)

        self.continue_button = QPushButton("Continue")
        self.continue_button.setCheckable(True)
        self.continue_button.setChecked(True)
        self.continue_button.clicked.connect(self.toggle_continue)
        self.continue_button.setFocusPolicy(Qt.NoFocus)
        self.continue_button.setToolTip("Toggle playback past shot boundaries")
        btn_grid.addWidget(self.continue_button, 2, 1)

        self.gremlins_button = QPushButton("👾 Gremlins")
        self.gremlins_button.setCheckable(True)
        self.gremlins_button.setChecked(False)
        self.gremlins_button.clicked.connect(self.toggle_gremlins)
        self.gremlins_button.setFocusPolicy(Qt.NoFocus)
        self.gremlins_button.setToolTip("Randomly jump movies/timecodes every second  [G]")
        btn_grid.addWidget(self.gremlins_button, 2, 2)

        right_layout.addLayout(btn_grid)

        # Keyboard hint
        hint = QLabel("↑↓ shot  Space play  ←→ frame  Shift+←→ 1s  PgUp/Dn scene  Home/End movie")
        hint.setFont(theme.font_mono())
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        right_layout.addWidget(hint)

        splitter.addWidget(left)
        splitter.addWidget(right)
        right.setMinimumWidth(350)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        self.rebuild_scene_list()
        self.setFocus()
    
    def update_timeline_slider(self):
        """Sync the timeline slider to the current frame without triggering a seek."""
        self._updating_slider = True
        self.timeline_slider.setValue(self.current_frame_number)
        self._updating_slider = False

    def on_timeline_seek(self, value: int):
        """Handle timeline slider movement — seek to that frame."""
        if self._updating_slider:
            return
        if self.is_playing:
            self.stop_playback()
        self.current_frame_number = value
        self.update_current_shot_from_frame()
        frame = self.get_frame(self.current_frame_number)
        if frame is not None:
            self.display_frame(frame)
        self.update_frame_info()

    def get_frame(self, frame_number: int) -> np.ndarray | None:
        """Extract a specific frame using OpenCV (frame-precise)."""
        if frame_number < 0 or frame_number >= self.total_frames:
            return None
        
        # Seek to exact frame
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        ret, frame = self.cap.read()
        
        if ret:
            return frame
        return None
    
    def display_frame(self, frame: np.ndarray):
        """Display an OpenCV frame in the Qt label."""
        if frame is None:
            self.frame_label.setText("Failed to load frame")
            return
        
        # Convert BGR to RGB
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Apply sample aspect ratio correction for non-square-pixel video
        if (self.sar_num, self.sar_den) != (1, 1):
            display_w = int(round(frame_rgb.shape[1] * self.sar_num / self.sar_den))
            frame_rgb = cv2.resize(frame_rgb, (display_w, frame_rgb.shape[0]),
                                   interpolation=cv2.INTER_LINEAR)

        # Convert to QImage
        height, width, channel = frame_rgb.shape
        bytes_per_line = 3 * width
        q_image = QImage(frame_rgb.data, width, height, bytes_per_line, QImage.Format_RGB888)
        
        # Convert to QPixmap and display
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
    
    def toggle_continue(self):
        """Toggle continue playback past shot boundaries."""
        self.continue_playback = not self.continue_playback

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
        # Switch movie silently (avoid save prompt during gremlins)
        if index != self.current_movie_index:
            if self.is_playing:
                self.stop_playback()
            if self.cap is not None:
                self.cap.release()
            self.current_movie_index = index
            self.filename = self.filenames[index]
            self.shots = []
            self.current_shot_index = 0
            self.modified = False
            self.current_frame_number = 0
            self.video_path = Path(self.project_path) / "media" / "videos" / self.media_type / self.filename
            if not self.video_path.exists():
                return
            self.cap = cv2.VideoCapture(str(self.video_path))
            if not self.cap.isOpened():
                return
            self.frame_rate = self.cap.get(cv2.CAP_PROP_FPS)
            self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
            raw_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.video_native_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self.sar_num, self.sar_den = _get_sar(str(self.video_path))
            self.video_native_width = int(round(raw_w * self.sar_num / self.sar_den))
            if self.frame_rate > 0:
                self.playback_timer.setInterval(int(1000 / self.frame_rate))
            self.timeline_slider.setMaximum(max(0, self.total_frames - 1))
            self.setWindowTitle(f"Shotlist Visualizer \u2014 {_display_name(self.filename)}  ({self.current_movie_index + 1}/{len(self.filenames)}) \ud83d\udc7e")
            self._updating_combo = True
            self.movie_combo.setCurrentIndex(index)
            self._updating_combo = False
            # Load shotlist for the new movie
            try:
                self.shots = read_shotlist(self.project_path, self.filename, self.media_type)
                for shot in self.shots:
                    if 'start_frame' in shot and isinstance(shot['start_frame'], str):
                        shot['start_frame'] = int(shot['start_frame'])
                    if 'end_frame' in shot and isinstance(shot['end_frame'], str):
                        shot['end_frame'] = int(shot['end_frame'])
            except FileNotFoundError:
                self.shots = []

        # Jump to a random shot index — reuses the exact same sync path as keyboard navigation
        if self.shots:
            shot_index = random.randrange(len(self.shots))
            # Restore is_playing so jump_to_shot resumes playback if we were playing
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

        # Compute the frame we should be on right now from elapsed real time
        elapsed = time.perf_counter() - self._play_start_time
        target_frame = self._play_start_frame + int(elapsed * self.frame_rate)
        target_frame = min(target_frame, self.total_frames - 1)

        # Skip if we haven't advanced yet (timer fired too early)
        if target_frame <= self.current_frame_number:
            return

        self.current_frame_number = target_frame

        # Stop if we reach end of video
        if self.current_frame_number >= self.total_frames:
            self.stop_playback()
            return
        
        # Check if we're still in continue mode or need to stop at shot boundary
        if not self.continue_playback:
            # Stop at end of current shot
            if 0 <= self.current_shot_index < len(self.shots):
                current_shot = self.shots[self.current_shot_index]
                end_frame = int(current_shot.get('end_frame', self.total_frames - 1))
                
                if self.current_frame_number > end_frame:
                    self.stop_playback()
                    return
        
        # Update which shot we're in (for continue mode or manual frame stepping)
        self.update_current_shot_from_frame()
        self.update_timeline_slider()

        # Display the frame
        frame = self.get_frame(self.current_frame_number)
        if frame is not None:
            self.display_frame(frame)
            
            # Update info label
            if 0 <= self.current_shot_index < len(self.shots):
                shot = self.shots[self.current_shot_index]
                start_tc = shot.get('start_time', '?')
                end_tc = shot.get('end_time', '?')
                confidence = shot.get('Shot_Confidence', '')
                conf_str = f"\nConfidence: {confidence}" if confidence else ""
                
                self.info_label.setText(
                    f"Scene {shot.get('Scene', '0')}  Shot #{self.current_shot_index}\n"
                    f"Frame: {self.current_frame_number}\n"
                    f"Timecode: {start_tc}→{end_tc}\n"
                    f"Status: PLAYING{conf_str}"
                )
    
    def update_current_shot_from_frame(self):
        """Update current shot index based on current frame number."""
        for i, shot in enumerate(self.shots):
            start_frame = int(shot.get('start_frame', 0))
            end_frame = int(shot.get('end_frame', 0))
            
            if start_frame <= self.current_frame_number <= end_frame:
                if i != self.current_shot_index:
                    self.current_shot_index = i
                    self.shot_list.setCurrentRow(i)
                    self.sync_scene_list_selection()
                return
    
    def rebuild_shot_list(self):
        """Rebuild the shot list widget from self.shots."""
        self.shot_list.clear()
        for i, shot in enumerate(self.shots):
            self.shot_list.addItem(ShotItem(i, shot))

    def rebuild_scene_list(self):
        """Rebuild the scene list from unique Scene values in self.shots."""
        self.scene_list.clear()
        seen = set()
        for shot in self.shots:
            scene = int(shot.get('Scene', 0))
            if scene not in seen:
                seen.add(scene)
                item = QListWidgetItem(str(scene))
                item.setData(Qt.UserRole, scene)
                item.setTextAlignment(Qt.AlignCenter)
                self.scene_list.addItem(item)

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

        was_playing = self.is_playing
        if self.is_playing:
            self.stop_playback()

        if self.cap is not None:
            self.cap.release()

        self.current_movie_index = index
        self.filename = self.filenames[index]
        self.shots = []
        self.current_shot_index = 0
        self.modified = False
        self.current_frame_number = 0

        self.video_path = Path(self.project_path) / "media" / "videos" / self.media_type / self.filename
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
            self.shots = read_shotlist(self.project_path, self.filename, self.media_type)
            for shot in self.shots:
                if 'start_frame' in shot and isinstance(shot['start_frame'], str):
                    shot['start_frame'] = int(shot['start_frame'])
                if 'end_frame' in shot and isinstance(shot['end_frame'], str):
                    shot['end_frame'] = int(shot['end_frame'])
        except FileNotFoundError as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.timeline_slider.setMaximum(max(0, self.total_frames - 1))
        self.timeline_slider.setValue(0)
        self.save_button.setEnabled(False)
        self.setWindowTitle(f"Shotlist Visualizer — {_display_name(self.filename)}  ({self.current_movie_index + 1}/{len(self.filenames)})")

        self._updating_combo = True
        self.movie_combo.setCurrentIndex(index)
        self._updating_combo = False

        self.rebuild_shot_list()
        self.rebuild_scene_list()
        self.update_stats()
        self.load_first_shot()
        if was_playing:
            self.start_playback()

    def sync_scene_list_selection(self):
        """Highlight the scene_list row matching the current shot's scene."""
        if not (0 <= self.current_shot_index < len(self.shots)):
            return
        current_scene = int(self.shots[self.current_shot_index].get('Scene', 0))
        for i in range(self.scene_list.count()):
            if self.scene_list.item(i).data(Qt.UserRole) == current_scene:
                self.scene_list.setCurrentRow(i)
                return

    def on_scene_selected(self, item: QListWidgetItem):
        """Jump to the first shot of the selected scene."""
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
        # Remember if we were playing
        was_playing = self.is_playing
        
        # Stop playback when jumping
        if self.is_playing:
            self.stop_playback()
        
        if 0 <= index < len(self.shots):
            self.current_shot_index = index
            shot = self.shots[index]
            
            # Get frame number
            if show_end:
                frame_number = int(shot.get('end_frame', 0))
                frame_type = "END"
            else:
                frame_number = int(shot.get('start_frame', 0))
                frame_type = "START"
            
            # Extract and display frame
            self.info_label.setText(f"Loading frame {frame_number}...")
            # NOTE: QApplication.processEvents() removed — it allowed timer callbacks
            # to fire re-entrantly while mid-switch, which was a crash hazard.

            # Update current frame number for playback
            self.current_frame_number = frame_number
            self.update_timeline_slider()

            frame = self.get_frame(frame_number)
            if frame is not None:
                self.display_frame(frame)
                
                # Update info label with shot details
                start_tc = shot.get('start_time', '?')
                end_tc = shot.get('end_time', '?')
                confidence = shot.get('Shot_Confidence', '')
                conf_str = f"\nConfidence: {confidence}" if confidence else ""
                
                self.info_label.setText(
                    f"Scene {shot.get('Scene', '0')}  Shot #{index}\n"
                    f"Frame: {frame_number}\n"
                    f"Timecode: {start_tc}→{end_tc}\n"
                    f"Viewing: {frame_type} frame{conf_str}"
                )
            else:
                self.info_label.setText(f"Failed to load frame {frame_number}")
            
            # Update selection in list
            self.shot_list.setCurrentRow(index)
            self.sync_scene_list_selection()
            self.update_buttons()
            
            # Resume playback if we were playing before
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
        next_frame_num = self.current_frame_number + 1
        if next_frame_num < self.total_frames:
            self.current_frame_number = next_frame_num
            self.update_current_shot_from_frame()
            self.update_timeline_slider()
            frame = self.get_frame(self.current_frame_number)
            if frame is not None:
                self.display_frame(frame)
                self.update_frame_info()

    def prev_frame(self):
        """Step backward one frame."""
        if self.is_playing:
            self.stop_playback()
        prev_frame_num = self.current_frame_number - 1
        if prev_frame_num >= 0:
            self.current_frame_number = prev_frame_num
            self.update_current_shot_from_frame()
            self.update_timeline_slider()
            frame = self.get_frame(self.current_frame_number)
            if frame is not None:
                self.display_frame(frame)
                self.update_frame_info()

    def step_seconds(self, direction: int):
        """Step forward or backward by approximately one second."""
        if self.is_playing:
            self.stop_playback()
        frames = max(1, int(round(self.frame_rate)))
        target = max(0, min(self.total_frames - 1, self.current_frame_number + frames * direction))
        self.current_frame_number = target
        self.update_current_shot_from_frame()
        self.update_timeline_slider()
        frame = self.get_frame(self.current_frame_number)
        if frame is not None:
            self.display_frame(frame)
        self.update_frame_info()
    
    def update_frame_info(self):
        """Update info label with current frame details."""
        if 0 <= self.current_shot_index < len(self.shots):
            shot = self.shots[self.current_shot_index]
            scene = shot.get('Scene', '0')
            start_tc = shot.get('start_time', '?')
            end_tc = shot.get('end_time', '?')
            confidence = shot.get('Shot_Confidence', '')
            conf_str = f"\nConfidence: {confidence}" if confidence else ""
            self.info_label.setText(
                f"Scene {scene}  Shot #{self.current_shot_index}\n"
                f"Frame: {self.current_frame_number}\n"
                f"Timecode: {start_tc}→{end_tc}\n"
                f"Status: STEPPING{conf_str}"
            )
    
    def on_shot_selected(self, item: ShotItem):
        """Handle shot selection from list."""
        self.jump_to_shot(item.index)
    
    def toggle_current_ignore(self):
        """Toggle ignore status for current shot."""
        if 0 <= self.current_shot_index < len(self.shots):
            item = self.shot_list.item(self.current_shot_index)
            if isinstance(item, ShotItem):
                item.toggle_ignore()
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
        
        # Update previous shot's end to current shot's end
        prev_shot['end_time'] = current_shot['end_time']
        prev_shot['end_frame'] = current_shot['end_frame']
        
        # Remove current shot
        self.shots.pop(self.current_shot_index)

        # Reindex annotation data to match new shot list
        reindex_annotations_for_merge(
            self.project_path, self.filename, self.media_type,
            self.current_shot_index,
        )

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

        # Build the two new shots
        first_shot = dict(shot)
        first_shot['end_frame'] = split_frame - 1
        first_shot['end_time'] = frames_to_timecode(split_frame - 1, self.frame_rate)
        first_shot['Shot_Caption'] = ''
        first_shot['Shot_Confidence'] = ''

        second_shot = dict(shot)
        second_shot['start_frame'] = split_frame
        second_shot['start_time'] = frames_to_timecode(split_frame, self.frame_rate)
        second_shot['Shot_Caption'] = ''
        second_shot['Shot_Source'] = 'manual'
        second_shot['Shot_Confidence'] = ''

        # Replace the current shot with the two new ones
        self.shots[self.current_shot_index:self.current_shot_index + 1] = [first_shot, second_shot]

        # Reindex annotation data to match new shot list
        reindex_annotations_for_split(
            self.project_path, self.filename, self.media_type,
            self.current_shot_index + 1,
        )

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
        self.shot_list.setCurrentRow(self.current_shot_index)
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
        self.shot_list.setCurrentRow(self.current_shot_index)
        self.sync_scene_list_selection()
        self.modified = True
        self.save_button.setEnabled(True)
        self.update_stats()

    def update_stats(self):
        """Update statistics display."""
        total_shots = len(self.shots)
        ignored_shots = sum(1 for shot in self.shots if shot.get('Ignore', 'No') == 'Yes')
        active_shots = total_shots - ignored_shots
        total_scenes = len(set(shot.get('Scene', '0') for shot in self.shots))
        self.stats_label.setText(
            f"Scenes: {total_scenes}  Shots: {total_shots}\n"
            f"Active: {active_shots}  Ignored: {ignored_shots}"
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
            if obj == self.shot_list and event.type() == QEvent.KeyPress:
                # Redirect keyboard events to main window
                key = event.key()
                if key in (Qt.Key_Space, Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down,
                          Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End,
                          Qt.Key_E, Qt.Key_F, Qt.Key_M, Qt.Key_N, Qt.Key_G):
                    # Handle it ourselves instead of letting the list widget process it
                    self.keyPressEvent(event)
                    return True  # Event handled, don't pass to list widget
            return super().eventFilter(obj, event)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return super().eventFilter(obj, event)
    
    def keyPressEvent(self, event):
        """Handle keyboard shortcuts."""
        key = event.key()
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
        elif key == Qt.Key_S and event.modifiers() & Qt.ControlModifier:
            if self.modified:
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
        # Stop gremlins and playback
        self.gremlins_timer.stop()
        if self.is_playing:
            self.stop_playback()
        
        # Release video capture
        if self.cap is not None:
            self.cap.release()
        
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

    # Enable low-level fault handler so C-level crashes (segfaults etc.) print a traceback
    faulthandler.enable()

    # Launch Qt application
    app = QApplication(sys.argv)
    theme.apply_theme(app)
    visualizer = OpenCVShotVisualizer(project_path, filenames, 0, args.media)

    # Open maximised on whatever screen the window appears on
    screen = QApplication.primaryScreen()
    avail = screen.availableGeometry()
    visualizer.setGeometry(avail)
    visualizer.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
