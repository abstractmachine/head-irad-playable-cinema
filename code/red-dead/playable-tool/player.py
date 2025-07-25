import os
import platform
import vlc

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget
)

SEEK_NORMAL = "1"
SEEK_FAST = "30"
FRAMES_PER_SHOT = 30

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

class PlayerWindow(QMainWindow):
    # Define signals for communication
    video_loaded = pyqtSignal(str)
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    video_timecode_changed = pyqtSignal(int)
    frames_extracted = pyqtSignal(list)
    
    # Internal signal for thread-safe VLC event handling
    _vlc_time_changed = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._pending_save_data = {}
        self.video_title = "[No video loaded]"
        self.timecode = "00:00:00"
        self.update_window_title()
        self.setGeometry(100, 100, 900, 600)
        self.current_video_path = None

        # VLC setup
        os.environ["VLC_VERBOSE"] = str("-1")
        self.vlc_instance = vlc.Instance()
        self.vlc_player = self.vlc_instance.media_player_new()
        self.video_widget = QWidget()

        # Set up VLC event callbacks for position changes
        self.vlc_events = self.vlc_player.event_manager()
        self.vlc_events.event_attach(vlc.EventType.MediaPlayerTimeChanged, self.on_vlc_time_changed_callback)
        self.vlc_events.event_attach(vlc.EventType.MediaPlayerPlaying, self.on_vlc_playing_callback)
        
        # Connect our internal signal to the main thread handler
        self._vlc_time_changed.connect(self.on_vlc_time_changed_main_thread)

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
        self._slider_is_active = False

        # Seek speed controls
        self.normal_seek = QLineEdit(SEEK_NORMAL)
        self.fast_seek = QLineEdit(SEEK_FAST)
        self.normal_seek.setToolTip("Normal seek speed in seconds")
        self.fast_seek.setToolTip("Fast seek speed in seconds")
        self.normal_seek.setFixedWidth(40)
        self.fast_seek.setFixedWidth(40)
        self.normal_seek.setFocusPolicy(Qt.ClickFocus)
        self.fast_seek.setFocusPolicy(Qt.ClickFocus)
        self.normal_seek.setAlignment(Qt.AlignCenter)
        self.fast_seek.setAlignment(Qt.AlignCenter)
        self.normal_seek.editingFinished.connect(self.validate_normal_seek)
        self.fast_seek.editingFinished.connect(self.validate_fast_seek)

        # Load button
        self.load_button = QPushButton("Load")
        self.load_button.setToolTip("Load video\nShortcut: L or V")
        self.load_button.clicked.connect(self.load_video)

        # Play/Pause button
        self.play_pause_button = QPushButton("Play")
        self.play_pause_button.setToolTip("Play or pause video\nShortcut:Space")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.play_pause_button.setEnabled(False)
        self.is_playing = False

        # Seek buttons
        self.back_button = QPushButton("Back")
        self.back_button.setToolTip("Seek backward\nShortcut: Left arrow, Shift for fast")
        self.back_button.setEnabled(False)
        self.back_button.clicked.connect(self.seek_back)
        self.forward_button = QPushButton("Forward")
        self.forward_button.setToolTip("Seek forward\nShortcut: Right arrow, Shift for fast")
        self.forward_button.setEnabled(False)
        self.forward_button.clicked.connect(self.seek_forward)

        # Make buttons expand equally
        for btn in [self.play_pause_button, self.back_button, self.forward_button]:
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Layouts
        seek_layout = QHBoxLayout()
        seek_layout.addWidget(QLabel("Seek:"))
        seek_layout.addWidget(self.normal_seek)
        seek_layout.addWidget(QLabel("Fast:"))
        seek_layout.addWidget(self.fast_seek)
        seek_layout.addStretch()

        # Set a fixed width for buttons
        button_width = 120
        for btn in [self.play_pause_button, self.back_button, self.forward_button]:
            btn.setFixedWidth(button_width)

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.play_pause_button)
        controls_layout.addStretch()
        controls_layout.addWidget(self.back_button)
        controls_layout.addStretch()
        controls_layout.addWidget(self.forward_button)
        controls_layout.addStretch()
        controls_layout.addLayout(seek_layout)

        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(self.video_widget)
        layout.addWidget(self.timeline)
        layout.addLayout(controls_layout)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Connect preference signals
        self.request_save.connect(self.on_request_save)
        self.request_load.connect(self.on_request_load)

        self._video_just_loaded = False

    def on_vlc_time_changed_callback(self, event):
        """VLC callback (runs in background thread) - emit signal to main thread"""
        self._vlc_time_changed.emit(event.u.new_time)

    def on_vlc_time_changed_main_thread(self, new_time):
        """Main thread handler for VLC time changes"""
        if (self._slider_is_active or 
            (hasattr(self.timeline, 'is_scrubbing') and self.timeline.is_scrubbing)):
            return
            
        self.timeline.setValue(new_time)
        self._update_timecode_display(new_time)
        self.emit_timecode_changed(new_time)

    def set_video_time(self, time_ms):
        """Single method to set VLC time and update all UI elements"""
        # Ensure time_ms is an integer
        time_ms = int(time_ms)
        
        duration = self.vlc_player.get_length()
        if duration > 0:
            time_ms = max(0, min(time_ms, duration))
        
        self.vlc_player.set_time(time_ms)
        self.timeline.setValue(time_ms)
        self._update_timecode_display(time_ms)
        self.emit_timecode_changed(time_ms)

    def _update_timecode_display(self, time_ms):
        """Convert milliseconds to HH:MM:SS and update display"""
        seconds = time_ms // 1000
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        self.set_timecode(f"{h:02}:{m:02}:{s:02}")

    def seek_video(self, seconds):
        """Seek video by specified number of seconds"""
        current_time = self.vlc_player.get_time()
        new_time = current_time + int(seconds * 1000)  # Convert to int
        self.set_video_time(new_time)

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

    def validate_normal_seek(self):
        try:
            value = float(self.normal_seek.text())
            if value <= 0:
                self.normal_seek.setText("1")
        except ValueError:
            self.normal_seek.setText("1")

    def validate_fast_seek(self):
        try:
            value = float(self.fast_seek.text())
            if value <= 0:
                self.fast_seek.setText("30")
        except ValueError:
            self.fast_seek.setText("30")

    def handle_global_key(self, event):
        """Handle global key events"""
        key = event.key()
        modifiers = event.modifiers()
        
        if key == Qt.Key_Space:
            if self.current_video_path and self.vlc_player:  # Only if video loaded
                self.toggle_play_pause()
        elif key == Qt.Key_L or key == Qt.Key_V:
            self.load_video()
        elif key == Qt.Key_Left:
            if not self.current_video_path or not self.vlc_player:  # Check before seeking
                return
            if modifiers & Qt.ShiftModifier:
                seek_amount = float(self.fast_seek.text())
                self.seek_video(-seek_amount)
            else:
                seek_amount = float(self.normal_seek.text())
                self.seek_video(-seek_amount)
        elif key == Qt.Key_Right:
            if not self.current_video_path or not self.vlc_player:  # Check before seeking
                return
            if modifiers & Qt.ShiftModifier:
                seek_amount = float(self.fast_seek.text())
                self.seek_video(seek_amount)
            else:
                seek_amount = float(self.normal_seek.text())
                self.seek_video(seek_amount)

    def on_request_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {
            "x": pos.x(),
            "y": pos.y(),
            "width": size.width(),
            "height": size.height(),
            "normal_seek": self.normal_seek.text(),
            "fast_seek": self.fast_seek.text()
        }

    def on_request_load(self, data):
        if "normal_seek" in data:
            self.normal_seek.setText(data["normal_seek"])
        if "fast_seek" in data:
            self.fast_seek.setText(data["fast_seek"])
        if "x" in data and "y" in data:
            self.move(data["x"], data["y"])
        if "width" in data and "height" in data:
            self.resize(data["width"], data["height"])

    def update_window_title(self):
        duration_seconds = getattr(self, "duration_seconds", None)
        if duration_seconds is not None and duration_seconds > 0:
            h = duration_seconds // 3600
            m = (duration_seconds % 3600) // 60
            s = duration_seconds % 60
            duration_str = f"{h:02}:{m:02}:{s:02}"
            self.setWindowTitle(f"{self.video_title} | {self.timecode} / {duration_str}")
        else:
            self.setWindowTitle(f"{self.video_title} | {self.timecode}")

    def load_video(self):
        """Load video using file dialog"""
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName(self, "Load Video", "", "Video Files (*.mp4 *.avi *.mov)")
        if file_path:
            self._load_video_file(file_path)

    def load_video_from_path(self, file_path):
        """Load video from a specific file path (called from cinema window)"""
        if file_path and os.path.exists(file_path):
            if hasattr(self, 'current_video_path') and self.current_video_path == file_path:
                return
            self._load_video_file(file_path)
        else:
            print(f"Cannot load video: file not found at {file_path}")

    def on_vlc_playing_callback(self, event):
        """Called when VLC starts playing"""
        if self._video_just_loaded:
            QTimer.singleShot(0, self._finalize_after_load)

    def _finalize_after_load(self):
        """Finalize video loading"""
        self.vlc_player.set_time(0)
        self.play_pause_button.setText("Pause")
        self.is_playing = True
        self._video_just_loaded = False
        self.video_loaded.emit(self.current_video_path)
        QTimer.singleShot(500, self._start_duration_polling)

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
        
        # Set video output to the widget
        win_id = int(self.video_widget.winId())
        if platform.system() == "Windows":
            self.vlc_player.set_hwnd(win_id)
        elif platform.system() == "Darwin":
            self.vlc_player.set_nsobject(win_id)
        else:
            self.vlc_player.set_xwindow(win_id)
        
        self.current_video_path = file_path
        
        # Enable controls
        self.play_pause_button.setEnabled(True)
        self.back_button.setEnabled(True)
        self.forward_button.setEnabled(True)
        self.timeline.setEnabled(True)
        
        self._video_just_loaded = True
        self.vlc_player.play()

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
            if hasattr(self, 'duration_timer') and self.duration_timer is not None:
                try:
                    self.duration_timer.stop()
                    self.duration_timer.deleteLater()
                except RuntimeError:
                    pass
                self.duration_timer = None

    def set_timecode(self, timecode):
        self.timecode = timecode
        self.update_window_title()

    def toggle_play_pause(self):
        if self.vlc_player.is_playing():
            self.vlc_player.pause()
            self.play_pause_button.setText("Play")
            self.is_playing = False
        else:
            self.vlc_player.play()
            self.play_pause_button.setText("Pause")
            self.is_playing = True

    def on_slider_pressed(self):
        """Called when user starts dragging the slider"""
        self._slider_is_active = True

    def on_slider_released(self):
        """Called when user releases the slider"""
        self._slider_is_active = False

    def handle_shot_timecodes(self, start_timecode, timecodes_list):
        """Handle shot timecodes from shotlist for frame extraction"""
        # Jump to the start of the shot
        self.jump_to_timecode(start_timecode)
        
        # Extract frames at the specified timecodes
        frames = []
        import cv2
        cap = cv2.VideoCapture(self.current_video_path)
        
        for timecode in timecodes_list:
            # Convert timecode to milliseconds
            parts = timecode.split(":")
            if len(parts) == 3:
                h = int(parts[0])
                m = int(parts[1])
                s = float(parts[2])
                time_ms = int((h * 3600 + m * 60 + s) * 1000)  # Ensure integer
                
                # Extract frame at this timecode
                cap.set(cv2.CAP_PROP_POS_MSEC, time_ms)
                ret, frame = cap.read()
                if ret:
                    frames.append(frame)
        
        cap.release()
        
        # Emit the extracted frames to the annotate window
        if frames:
            self.frames_extracted.emit(frames)
            print(f"Extracted {len(frames)} frames from shot")
        else:
            print("No frames could be extracted from the shot")

    def emit_timecode_changed(self, position):
        self.video_timecode_changed.emit(position)