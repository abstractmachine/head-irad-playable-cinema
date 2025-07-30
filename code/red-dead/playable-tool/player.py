from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QSizePolicy, QSlider
)

import os
import platform
import vlc

SEEK_NORMAL = "1"
SEEK_FAST = "30"
FRAMES_PER_SHOT = 30

class AbstractPlayerWindow(QMainWindow):
    # Signals for communication
    video_loaded_with_metadata = pyqtSignal(str, dict)
    video_timecode_changed = pyqtSignal(int)
    frames_extracted = pyqtSignal(list)
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        self._pending_save_data = {}
        self.video_title = ""
        self.movie_metadata = None
        self.timecode = "00:00:00"
        self.current_video_path = None
        self.duration_seconds = None
        self._slider_is_active = False

        # VLC setup (subclasses may override for other backends)
        os.environ["VLC_VERBOSE"] = str("-1")
        self.vlc_instance = vlc.Instance()
        self.vlc_player = self.vlc_instance.media_player_new()
        self.video_widget = QWidget()
        self.video_widget.setStyleSheet("background-color: black;")

        # Timeline slider
        self.timeline = JumpSlider(Qt.Horizontal)
        self.timeline.player_window = self
        self.timeline.setRange(0, 100)
        self.timeline.setValue(0)
        self.timeline.setEnabled(False)
        self.timeline.setMinimumHeight(32)
        self.timeline.setToolTip("Scrub through the video timeline")
        self.timeline.sliderPressed.connect(self.on_slider_pressed)
        self.timeline.sliderReleased.connect(self.on_slider_released)

        button_width, button_height = self.ui.get_dimensions('button')

        # Seek speed controls
        self.normal_seek = QLineEdit("1")
        self.fast_seek = QLineEdit("30")
        tiny_width, tiny_height = self.ui.get_dimensions('tiny')
        self.normal_seek.setFixedSize(tiny_width, tiny_height)
        self.fast_seek.setFixedSize(tiny_width, tiny_height)
        self.normal_seek.setFont(self.ui.get_font('tiny'))
        self.fast_seek.setFont(self.ui.get_font('tiny'))
        self.normal_seek.setAlignment(Qt.AlignCenter)
        self.fast_seek.setAlignment(Qt.AlignCenter)
        self.normal_seek.editingFinished.connect(self.validate_normal_seek)
        self.fast_seek.editingFinished.connect(self.validate_fast_seek)
        self.normal_seek.setFocusPolicy(Qt.ClickFocus)
        self.fast_seek.setFocusPolicy(Qt.ClickFocus)

        # Play/Pause button
        self.play_pause_button = QPushButton("⏵")
        self.play_pause_button.setToolTip("Play or pause video\nShortcut:Space")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.play_pause_button.setEnabled(False)
        self.play_pause_button.setFont(self.ui.get_font('button'))
        self.play_pause_button.setFixedSize(60, button_height)
        self.is_playing = False

        # Seek back
        self.back_button = QPushButton("⏪")
        self.back_button.setToolTip("Seek backward\nShortcut: Left arrow, Shift for fast")
        self.back_button.setEnabled(False)
        self.back_button.clicked.connect(self.seek_back)
        self.back_button.setFont(self.ui.get_font('button'))
        self.back_button.setFixedSize(60, button_height)

        # Seek forward
        self.forward_button = QPushButton("⏩")
        self.forward_button.setToolTip("Seek forward\nShortcut: Right arrow, Shift for fast")
        self.forward_button.setEnabled(False)
        self.forward_button.clicked.connect(self.seek_forward)
        self.forward_button.setFont(self.ui.get_font('button'))
        self.forward_button.setFixedSize(60, button_height)

        # Timecode display
        self.timecode_label = QLabel("00:00:00 | 00:00:00")
        self.timecode_label.setFont(self.ui.get_font('monospace'))
        self.timecode_label.setAlignment(Qt.AlignCenter)
        self.timecode_label.setStyleSheet("QLabel { padding: 0px; }")
        self.timecode_label.setToolTip("Current timecode | Total duration")

        # Layouts
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.play_pause_button)
        controls_layout.addWidget(self.back_button)
        controls_layout.addWidget(self.forward_button)
        controls_layout.addWidget(self.normal_seek)
        controls_layout.addWidget(self.fast_seek)
        controls_layout.addStretch()
        controls_layout.addWidget(self.timecode_label)

        layout = QVBoxLayout()
        layout.setSpacing(5)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.addWidget(self.video_widget, stretch=1)
        layout.addWidget(self.timeline)
        layout.addLayout(controls_layout)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Duration polling
        self.duration_timer = None

    def update_window_title(self):
        """Update window title to show {title} ({year})"""
        titlebar_string = "Player"
        if self.video_title == "":
            titlebar_string = "Player"
        elif self.movie_metadata:
            title = self.movie_metadata.get('title', 'Unknown Title')
            year = self.movie_metadata.get('year', '')
            if year:
                titlebar_string = f"{title} ({year})"
            else:
                titlebar_string = title
        else:
            titlebar_string = f"Player | {self.video_title}"
        self.setWindowTitle(titlebar_string)

    def load_video_from_path_with_metadata(self, file_path, metadata=None):
        """Load video from a specific file path with metadata"""
        if file_path and os.path.exists(file_path):
            if hasattr(self, 'current_video_path') and self.current_video_path == file_path:
                return
            self.movie_metadata = metadata
            self._load_video_file(file_path)
        else:
            print(f"Cannot load video: file not found at {file_path}")

    def _load_video_file(self, file_path):
        """Internal method to handle the actual video loading process"""
        self.timeline.setValue(0)
        self.set_timecode("00:00:00")
        if hasattr(self, 'vlc_player') and self.vlc_player:
            self.vlc_player.stop()
        if hasattr(self, 'duration_timer') and self.duration_timer is not None:
            try:
                self.duration_timer.stop()
                self.duration_timer.deleteLater()
            except RuntimeError:
                pass
            self.duration_timer = None
        self.video_title = os.path.basename(file_path)
        self.update_window_title()
        media = self.vlc_instance.media_new(file_path)
        self.vlc_player.set_media(media)
        win_id = int(self.video_widget.winId())
        if platform.system() == "Windows":
            self.vlc_player.set_hwnd(win_id)
        elif platform.system() == "Darwin":
            self.vlc_player.set_nsobject(win_id)
        else:
            self.vlc_player.set_xwindow(win_id)
        self.current_video_path = file_path
        self.play_pause_button.setEnabled(True)
        self.back_button.setEnabled(True)
        self.forward_button.setEnabled(True)
        self.timeline.setEnabled(True)
        # self.vlc_player.play()
        self.play_pause_button.setText("⏵")
        self.is_playing = False

        # Prime VLC so seeking works before first play
        self.vlc_player.play()
        QTimer.singleShot(100, self.vlc_player.pause)

        QTimer.singleShot(500, self._start_duration_polling)
        self.video_loaded_with_metadata.emit(self.current_video_path, self.movie_metadata)

        # VLC event: emit timecode signal as video plays
        event_manager = self.vlc_player.event_manager()
        event_manager.event_attach(
            vlc.EventType.MediaPlayerTimeChanged,
            self._on_vlc_time_changed
        )

    def _start_duration_polling(self):
        """Start duration polling"""
        if hasattr(self, 'duration_timer') and self.duration_timer is not None:
            try:
                self.duration_timer.stop()
                self.duration_timer.deleteLater()
            except RuntimeError:
                pass
        self.duration_timer = QTimer(self)
        self.duration_timer.setInterval(200)
        self.duration_timer.timeout.connect(self.check_duration)
        self.duration_timer.start()

    def check_duration(self):
        duration = self.vlc_player.get_length()
        if duration and duration > 0:
            self.timeline.setRange(0, duration)
            self.duration_seconds = duration // 1000
            self.update_window_title()
            self._update_timecode_display(self.vlc_player.get_time())
            if hasattr(self, 'duration_timer') and self.duration_timer is not None:
                try:
                    self.duration_timer.stop()
                    self.duration_timer.deleteLater()
                except RuntimeError:
                    pass
                self.duration_timer = None

    def set_timecode(self, timecode):
        self.timecode = timecode

    def toggle_play_pause(self):
        if self.vlc_player.is_playing():
            self.vlc_player.pause()
            self.play_pause_button.setText("⏵")
            self.is_playing = False
        else:
            self.vlc_player.play()
            self.play_pause_button.setText("⏸")
            self.is_playing = True

    def seek_back(self):
        if not self.current_video_path or not self.vlc_player:
            return
        seek_amount = float(self.normal_seek.text())
        self.seek_video(-seek_amount)

    def seek_forward(self):
        if not self.current_video_path or not self.vlc_player:
            return
        seek_amount = float(self.normal_seek.text())
        self.seek_video(seek_amount)

    def seek_video(self, seconds):
        current_time = self.vlc_player.get_time()
        new_time = current_time + int(seconds * 1000)
        self.set_video_time(new_time)

    def set_video_time(self, time_ms):
        time_ms = int(time_ms)
        duration = self.vlc_player.get_length()
        if duration > 0:
            time_ms = max(0, min(time_ms, duration))
        self.vlc_player.set_time(time_ms)
        self.timeline.setValue(time_ms)
        self._update_timecode_display(time_ms)
        self.emit_timecode_changed(time_ms)

    def jump_to_timecode(self, timecode, is_last_frame=False):
        """Jump to specific timecode"""
        parts = timecode.split(":")
        if len(parts) == 3:
            h = int(parts[0])
            m = int(parts[1])
            s = float(parts[2])
            time_ms = int((h * 3600 + m * 60 + s) * 1000)
            self.set_video_time(time_ms)
        else:
            print(f"Invalid timecode format: {timecode}")

    def _update_timecode_display(self, time_ms):
        seconds = time_ms // 1000
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        self.set_timecode(f"{h:02}:{m:02}:{s:02}")
        duration_seconds = getattr(self, "duration_seconds", None)
        if duration_seconds is not None and duration_seconds > 0:
            dh = duration_seconds // 3600
            dm = (duration_seconds % 3600) // 60
            ds = duration_seconds % 60
            duration_str = f"{dh:02}:{dm:02}:{ds:02}"
            self.timecode_label.setText(f"{h:02}:{m:02}:{s:02} | {duration_str}")
        else:
            self.timecode_label.setText(f"{h:02}:{m:02}:{s:02} | 00:00:00")

    def handle_shot_timecodes(self, start_timecode, timecodes_list):
        """Handle shot timecodes from shotlist for frame extraction"""
        
        if not self.current_video_path:
            return
            
        # Jump to the start of the shot
        self.jump_to_timecode(start_timecode)
        
        # Extract frames at the specified timecodes
        frames = []
        import cv2
        cap = cv2.VideoCapture(self.current_video_path)
        
        for i, timecode in enumerate(timecodes_list):
            # Convert timecode to milliseconds
            parts = timecode.split(":")
            if len(parts) == 3:
                h = int(parts[0])
                m = int(parts[1])
                s = float(parts[2])
                time_ms = int((h * 3600 + m * 60 + s) * 1000)
                                
                # Extract frame at this timecode
                cap.set(cv2.CAP_PROP_POS_MSEC, time_ms)
                ret, frame = cap.read()
                if ret:
                    frames.append(frame)
                else:
                    pass

        cap.release()
        
        # Emit the extracted frames to the annotate window
        if frames:
            self.frames_extracted.emit(frames)
        else:
            pass

    def emit_timecode_changed(self, position):
        self.video_timecode_changed.emit(position)

    def on_slider_pressed(self):
        self._slider_is_active = True

    def on_slider_released(self):
        self._slider_is_active = False

    def validate_normal_seek(self):
        try:
            value = float(self.normal_seek.text())
            if value <= 0:
                self.normal_seek.setText("1")
        except ValueError:
            self.normal_seek.setText("1")
            
    def _on_vlc_time_changed(self, event):
        """Emit signal when VLC time changes (for subtitles, etc.)"""
        position = self.vlc_player.get_time()
        self.timeline.setValue(position)
        self._update_timecode_display(position)
        self.emit_timecode_changed(position)

    def validate_fast_seek(self):
        try:
            value = float(self.fast_seek.text())
            if value <= 0:
                self.fast_seek.setText("30")
        except ValueError:
            self.fast_seek.setText("30")

    def closeEvent(self, event):
        try:
            if hasattr(self, 'vlc_player') and self.vlc_player:
                self.vlc_player.stop()
            if hasattr(self, 'vlc_player') and self.vlc_player:
                event_manager = self.vlc_player.event_manager()
                if event_manager:
                    event_manager.event_detach(vlc.EventType.MediaPlayerTimeChanged)
                    event_manager.event_detach(vlc.EventType.MediaPlayerPlaying)
                self.vlc_player.release()
                self.vlc_player = None
            if hasattr(self, 'vlc_instance') and self.vlc_instance:
                self.vlc_instance.release()
                self.vlc_instance = None
        except Exception as e:
            print(f"Error during cleanup: {e}")
        finally:
            super().closeEvent(event)

    def on_request_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {
            "normal_seek": self.normal_seek.text(),
            "fast_seek": self.fast_seek.text()
        }

    def on_request_load(self, data):
        if "normal_seek" in data:
            self.normal_seek.setText(data["normal_seek"])
        if "fast_seek" in data:
            self.fast_seek.setText(data["fast_seek"])

class JumpSlider(QSlider):
    def __init__(self, orientation):
        super().__init__(orientation)
        self.player_window = None
        self.is_scrubbing = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.is_scrubbing = True
            self._jump_to_mouse_position(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.is_scrubbing and event.buttons() & Qt.LeftButton:
            self._jump_to_mouse_position(event)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.is_scrubbing = False
            self._jump_to_mouse_position(event, immediate=True)
        super().mouseReleaseEvent(event)

    def _jump_to_mouse_position(self, event, immediate=False):
        if self.orientation() == Qt.Horizontal:
            if hasattr(event, "position"):
                x = event.position().x()
            else:
                x = event.x()
            value = self.minimum() + ((self.maximum() - self.minimum()) * x) / self.width()
        else:
            if hasattr(event, "position"):
                y = event.position().y()
            else:
                y = event.y()
            value = self.minimum() + ((self.maximum() - self.minimum()) * (self.height() - y)) / self.height()
        
        value = max(self.minimum(), min(self.maximum(), int(value)))
        
        if self.player_window:
            if immediate:
                self.player_window.set_video_time(value)
            else:
                self.setValue(value)
                self.player_window._update_timecode_display(value)
                self.player_window.emit_timecode_changed(value)
                self.player_window.vlc_player.set_time(value)
