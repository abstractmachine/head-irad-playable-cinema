import os
import cv2
import platform
import vlc

from PyQt5.QtCore import Qt, pyqtSignal, QObject, QThread, QTimer, QMetaObject, Q_ARG
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget, QStyle
)

SEEK_NORMAL = "1"
SEEK_FAST = "30"
FRAMES_PER_SHOT = 30  # Assuming a default value, adjust as necessary

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
        self.timeline.player_window = self  # Give slider reference to player
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
        for btn in [self.load_button, self.play_pause_button, self.back_button, self.forward_button]:
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
        for btn in [self.load_button, self.play_pause_button, self.back_button, self.forward_button]:
            btn.setFixedWidth(button_width)

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.load_button)
        controls_layout.addStretch()
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
        # This runs in VLC's thread, so we emit a signal to get back to main thread
        self._vlc_time_changed.emit(event.u.new_time)

    def on_vlc_time_changed_main_thread(self, new_time):
        """Main thread handler for VLC time changes"""
        # Don't update anything during active scrubbing to avoid conflicts
        if (self._slider_is_active or 
            (hasattr(self.timeline, 'is_scrubbing') and self.timeline.is_scrubbing)):
            return
            
        # Only update if we're not actively scrubbing
        self.timeline.setValue(new_time)
        
        # Update timecode display
        seconds = new_time // 1000
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        self.set_timecode(f"{h:02}:{m:02}:{s:02}")
        self.emit_timecode_changed(new_time)

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

    def update_duration(self):
        # VLC: get duration in ms
        duration = self.vlc_player.get_length()
        if duration > 0:
            self.timeline.setRange(0, duration)
            self.duration_seconds = duration // 1000
        self.update_window_title()

    def load_video(self):
        """Load video using file dialog"""
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName(self, "Load Video", "", "Video Files (*.mp4 *.avi *.mov)")
        if file_path:
            self._load_video_file(file_path)

    def load_video_from_path(self, file_path):
        """Load video from a specific file path (called from cinema window)"""
        if file_path and os.path.exists(file_path):
            # Check if this is the same video that's already loaded
            if hasattr(self, 'current_video_path') and self.current_video_path == file_path:
                # print(f"Video already loaded: {file_path}")
                return
            
            self._load_video_file(file_path)
        else:
            print(f"Cannot load video: file not found at {file_path}")

    def on_vlc_playing_callback(self, event):
        """Called when VLC starts playing - let it continue playing"""
        if self._video_just_loaded:
            # This runs in VLC thread, so use QTimer to get back to main thread
            QTimer.singleShot(0, self._finalize_after_load)

    def _finalize_after_load(self):
        """Finalize video loading without pausing (main thread)"""
        # DON'T pause - let it keep playing
        # self.vlc_player.pause()  # Remove this line
        self.vlc_player.set_time(0)  # Still start from beginning
        
        # Update UI state to reflect that it's playing
        self.play_pause_button.setText("Pause")
        self.is_playing = True
        
        self._video_just_loaded = False
        
        # Emit the signal and start duration polling
        self.video_loaded.emit(self.current_video_path)
        QTimer.singleShot(500, self._start_duration_polling)

    def _load_video_file(self, file_path):
        """Internal method to handle the actual video loading process"""
        # print(f"Loading video file: {file_path}")
        
        # Reset timeline to 0 immediately when starting to load
        self.timeline.setValue(0)
        self.set_timecode("00:00:00")
    
        # Stop any existing playback first
        if hasattr(self, 'vlc_player') and self.vlc_player:
            self.vlc_player.stop()
        
        # Stop any existing duration timer more safely
        if hasattr(self, 'duration_timer') and self.duration_timer is not None:
            try:
                self.duration_timer.stop()
                self.duration_timer.deleteLater()
            except RuntimeError:
                # Timer was already deleted, ignore the error
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
        
        # Enable controls immediately
        self.play_pause_button.setEnabled(True)
        self.back_button.setEnabled(True)
        self.forward_button.setEnabled(True)
        self.timeline.setEnabled(True)
        
        # Set flag that we just loaded a video
        self._video_just_loaded = True
        
        # Start video
        self.vlc_player.play()

    def _start_duration_polling(self):
        """Start duration polling with proper cleanup"""
        # Clean up any existing timer first
        if hasattr(self, 'duration_timer') and self.duration_timer is not None:
            try:
                self.duration_timer.stop()
                self.duration_timer.deleteLater()
            except RuntimeError:
                pass
    
        # Create new timer
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
            # Safely stop and clean up the timer
            if hasattr(self, 'duration_timer') and self.duration_timer is not None:
                try:
                    self.duration_timer.stop()
                    self.duration_timer.deleteLater()
                except RuntimeError:
                    pass
                self.duration_timer = None
            # print(f"Duration set: {duration}ms ({self.duration_seconds}s)")
        # else:
        #     print("Waiting for valid duration...")

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

    def emit_timecode_changed(self, position):
        self.video_timecode_changed.emit(position)

    def seek_video(self, seconds):
        """Seek video by specified number of seconds"""
        current_time = self.vlc_player.get_time()
        new_position = current_time + (seconds * 1000)
        duration = self.vlc_player.get_length()
        
        # Clamp to valid range
        new_position = max(0, min(new_position, duration))
        self.vlc_player.set_time(new_position)

    def seek_back(self):
        """Seek backward by normal seek amount"""
        normal_seek = int(self.normal_seek.text() or "1")
        self.seek_video(-normal_seek)

    def seek_forward(self):
        """Seek forward by normal seek amount"""
        normal_seek = int(self.normal_seek.text() or "1")
        self.seek_video(normal_seek)

    def validate_normal_seek(self):
        value = self.normal_seek.text()
        if not value.isdigit() or int(value) <= 0:
            self.normal_seek.setText("1")
        self.normal_seek.clearFocus()

    def validate_fast_seek(self):
        value = self.fast_seek.text()
        if not value.isdigit() or int(value) <= 0:
            self.fast_seek.setText("10")
        self.fast_seek.clearFocus()

    def jump_to_timecode(self, timecode, is_last_frame=False):
        parts = timecode.split(":")
        if len(parts) == 3:
            h = int(parts[0])
            m = int(parts[1])
            s = float(parts[2])
            ms = int((h * 3600 + m * 60 + s) * 1000)
            # is_last_frame logic unchanged
            self.vlc_player.set_time(ms)
        else:
            print(f"Invalid timecode format: {timecode}")

    def handle_global_key(self, event):
        focus_widget = QApplication.focusWidget()
        if not isinstance(focus_widget, QLineEdit):
            self.keyPressEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        normal_seek = int(self.normal_seek.text() or "1")
        fast_seek = int(self.fast_seek.text() or "10")
        seek_amount = normal_seek

        if modifiers & Qt.ShiftModifier:
            seek_amount = fast_seek

        if key in (Qt.Key_L, Qt.Key_V):
            self.load_video()
        elif key == Qt.Key_Space:
            if self.play_pause_button.isEnabled():
                self.toggle_play_pause()
        elif key == Qt.Key_Left:
            if self.back_button.isEnabled():
                self.seek_video(-seek_amount)
        elif key == Qt.Key_Right:
            if self.forward_button.isEnabled():
                self.seek_video(seek_amount)

    def closeEvent(self, event):
        try:
            self.vlc_player.stop()
        except Exception:
            pass
        super().closeEvent(event)

    def update_position_from_slider(self):
        self.vlc_player.set_time(self.timeline.value())

    def extract_frames_for_timecodes(self, timecodes):
        # Extract frames for the given timecodes from the current video.
        # Returns a list of numpy arrays (frames).

        if not self.current_video_path:
            print("No video loaded for frame extraction.")
            return []

        cap = cv2.VideoCapture(self.current_video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = []

        def timecode_to_frame(tc):
            parts = tc.split(":")
            if len(parts) == 3:
                h = int(parts[0])
                m = int(parts[1])
                s = float(parts[2])
                total_seconds = h * 3600 + m * 60 + s
                return int(total_seconds * fps)
            return None

        for tc in timecodes:
            frame_num = timecode_to_frame(tc)
            if frame_num is not None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
                ret, frame = cap.read()
                if ret:
                    frames.append(frame)
                else:
                    print(f"Failed to extract frame at {tc} (frame {frame_num})")
            else:
                print(f"Invalid timecode: {tc}")

        cap.release()
        return frames

    def handle_shot_timecodes(self, start_tc, timecodes):
        # print(f"Handling shot timecodes: start={start_tc}, count={len(timecodes)}")
        # all_timecodes = [start_tc] + timecodes
        all_timecodes = timecodes  # Use only the provided timecodes, not including start_tc
        self.frame_thread = QThread()
        self.frame_worker = FrameExtractorWorker(self.current_video_path, all_timecodes)
        self.frame_worker.moveToThread(self.frame_thread)
        self.frame_thread.started.connect(self.frame_worker.run)
        self.frame_worker.finished.connect(self.handle_frames_extracted)
        self.frame_worker.finished.connect(self.frame_thread.quit)
        self.frame_worker.finished.connect(self.frame_worker.deleteLater)
        self.frame_thread.finished.connect(self.frame_thread.deleteLater)
        self.frame_thread.start()

    def handle_frames_extracted(self, frames):
        # print(f"Extracted {len(frames)} frames for API.")
        self.frames_extracted.emit(frames)  # Emit frames to AnnotateWindow

class FrameExtractorWorker(QObject):
    finished = pyqtSignal(list)  # Emits list of frames

    def __init__(self, video_path, timecodes):
        super().__init__()
        self.video_path = video_path
        self.timecodes = timecodes

    def run(self):
        import cv2
        cap = cv2.VideoCapture(self.video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = []

        def timecode_to_frame(tc):
            parts = tc.split(":")
            if len(parts) == 3:
                h = int(parts[0])
                m = int(parts[1])
                s = float(parts[2])
                total_seconds = h * 3600 + m * 60 + s
                return int(total_seconds * fps)
            return None

        for tc in self.timecodes:
            frame_num = timecode_to_frame(tc)
            if frame_num is not None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
                ret, frame = cap.read()
                if ret:
                    frames.append(frame)
        cap.release()
        self.finished.emit(frames)

class JumpSlider(QSlider):
    """Custom slider that supports real-time scrubbing like QuickTime"""
    
    def __init__(self, orientation):
        super().__init__(orientation)
        self.was_playing_before_scrub = False
        self.player_window = None  # Will be set by PlayerWindow
        self.is_scrubbing = False
        self.last_seek_time = 0  # Throttle seeking during scrub
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.player_window:
            self.is_scrubbing = True
            
            # Remember if we were playing before scrubbing
            self.was_playing_before_scrub = self.player_window.vlc_player.is_playing()
            
            # Pause immediately
            if self.was_playing_before_scrub:
                self.player_window.vlc_player.pause()
                self.player_window.play_pause_button.setText("Play")
                self.player_window.is_playing = False
            
            # Calculate and jump to clicked position
            self._jump_to_mouse_position(event, immediate=True)
            
            self.sliderPressed.emit()
        
        super().mousePressEvent(event)
    
    def mouseMoveEvent(self, event):
        # If we're dragging (mouse is pressed), jump to new position
        if event.buttons() & Qt.LeftButton and self.player_window and self.is_scrubbing:
            # Throttle the seeking during scrub to every 50ms for better performance
            import time
            current_time = time.time() * 1000
            if current_time - self.last_seek_time > 50:  # Only seek every 50ms
                self._jump_to_mouse_position(event, immediate=False)
                self.last_seek_time = current_time
            else:
                # Still update slider position for smooth visual feedback
                self._update_slider_position_only(event)
        
        super().mouseMoveEvent(event)
    
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.player_window and self.is_scrubbing:
            # Jump to final position
            self._jump_to_mouse_position(event, immediate=True)
            
            # Restore previous playback state
            if self.was_playing_before_scrub:
                self.player_window.vlc_player.play()
                self.player_window.play_pause_button.setText("Pause")
                self.player_window.is_playing = True
            
            self.is_scrubbing = False
            self.sliderReleased.emit()
        
        super().mouseReleaseEvent(event)
    
    def _update_slider_position_only(self, event):
        """Update only the slider position without seeking video"""
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
        
        # Clamp value to valid range
        value = max(self.minimum(), min(self.maximum(), int(value)))
        
        # Update slider position only
        self.setValue(value)
        
        # Update timecode display without seeking
        if self.player_window:
            seconds = value // 1000
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            self.player_window.set_timecode(f"{h:02}:{m:02}:{s:02}")
    
    def _jump_to_mouse_position(self, event, immediate=False):
        """Calculate position based on mouse and jump to that timecode"""
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
        
        # Clamp value to valid range
        value = max(self.minimum(), min(self.maximum(), int(value)))
        
        # Update slider position
        self.setValue(value)
        
        # Update timecode display
        if self.player_window:
            seconds = value // 1000
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            self.player_window.set_timecode(f"{h:02}:{m:02}:{s:02}")
            
            # Only seek video for immediate actions (click/release) or throttled moves
            if immediate:
                # Use position-based seeking for better performance
                duration = self.player_window.vlc_player.get_length()
                if duration > 0:
                    position = value / duration
                    self.player_window.vlc_player.set_position(position)
            else:
                # For drag moves, use faster time-based seeking
                self.player_window.vlc_player.set_time(value)