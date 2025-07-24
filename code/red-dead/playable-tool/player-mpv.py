import os
import ctypes.util
import subprocess

# Improved MPV library detection for macOS
def find_and_set_libmpv():
    # Method 1: Try standard detection
    libmpv_path = ctypes.util.find_library('mpv')
    if libmpv_path:
        print(f"Found libmpv via ctypes: {libmpv_path}")
        return True
    
    # Method 2: Use brew to find the exact path (FIXED)
    try:
        result = subprocess.run(['brew', '--prefix', 'mpv'], capture_output=True, text=True, check=True)
        mpv_prefix = result.stdout.strip()
        possible_paths = [
            f'{mpv_prefix}/lib/libmpv.dylib',      # This is the correct path!
            f'{mpv_prefix}/lib/libmpv.1.dylib',
            f'{mpv_prefix}/lib/libmpv.2.dylib'
        ]
        for path in possible_paths:
            if os.path.exists(path):
                os.environ['LIBMPV_PATH'] = path
                print(f"Found libmpv via brew: {path}")
                return True
    except subprocess.CalledProcessError:
        pass
    except FileNotFoundError:
        print("Homebrew not found")
    
    # Method 3: Check common Homebrew locations (UPDATED)
    brew_prefixes = ['/opt/homebrew/opt/mpv', '/usr/local/opt/mpv', '/opt/homebrew', '/usr/local']
    for prefix in brew_prefixes:
        possible_paths = [
            f'{prefix}/lib/libmpv.dylib',
            f'{prefix}/lib/libmpv.1.dylib',
            f'{prefix}/lib/libmpv.2.dylib'
        ]
        for path in possible_paths:
            if os.path.exists(path):
                os.environ['LIBMPV_PATH'] = path
                print(f"Found libmpv at: {path}")
                return True
    
    print("Could not find libmpv library")
    return False

# Try to find and set the library path BEFORE importing mpv
if not find_and_set_libmpv():
    print("Error: libmpv not found. Please check your MPV installation:")
    print("  brew reinstall mpv")
    raise ImportError("libmpv library not found")

# NOW import mpv after setting the path
import cv2
import mpv
import time

from PyQt5.QtCore import Qt, pyqtSignal, QObject, QThread, QTimer
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget
)

SEEK_NORMAL = "1"
SEEK_FAST = "30"
FRAMES_PER_SHOT = 30

class PlayerWindow(QMainWindow):

    # Define signals for communication
    video_loaded = pyqtSignal(str)
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    video_timecode_changed = pyqtSignal(int)
    frames_extracted = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._pending_save_data = {}
        self.video_title = "[No video loaded]"
        self.timecode = "00:00:00"
        self.update_window_title()
        self.setGeometry(100, 100, 900, 600)
        self.current_video_path = None

        # MPV setup
        self.video_widget = QWidget()
        self.player = None
        self._setup_player()

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

        # Play/Pause button
        self.play_pause_button = QPushButton("Play")
        self.play_pause_button.setToolTip("Play or pause video\nShortcut: Space")
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

        # Timer for position updates
        self.position_timer = QTimer()
        self.position_timer.timeout.connect(self.update_position)
        self.position_timer.setInterval(100)

    def _setup_player(self):
        """Initialize MPV player"""
        try:
            self.player = mpv.MPV(
                wid=str(int(self.video_widget.winId())),
                vo='gpu',  # Use GPU acceleration
                hwdec='auto',  # Hardware decoding
                pause=True,  # Start paused
                keep_open=True,  # Keep window open after playback
                idle=True  # Stay idle when no file is loaded
            )
            
            # Set up property observers
            self.player.observe_property('time-pos', self.on_time_changed)
            self.player.observe_property('duration', self.on_duration_changed)
            self.player.observe_property('pause', self.on_pause_changed)
            
        except Exception as e:
            print(f"Failed to initialize MPV: {e}")
            self.player = None

    def on_time_changed(self, name, value):
        """Called when playback position changes"""
        if value is not None and not self._slider_is_active and hasattr(self.timeline, 'is_scrubbing') and not self.timeline.is_scrubbing:
            ms = int(value * 1000)
            self.timeline.setValue(ms)
            
            # Update timecode display
            seconds = int(value)
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            self.set_timecode(f"{h:02}:{m:02}:{s:02}")
            self.emit_timecode_changed(ms)

    def on_duration_changed(self, name, value):
        """Called when video duration is available"""
        if value is not None:
            duration_ms = int(value * 1000)
            self.timeline.setRange(0, duration_ms)
            self.duration_seconds = int(value)
            self.update_window_title()

    def on_pause_changed(self, name, value):
        """Called when pause state changes"""
        if value is not None:
            self.is_playing = not value
            self.play_pause_button.setText("Pause" if self.is_playing else "Play")

    def update_position(self):
        """Timer-based position update for smooth scrubbing"""
        if self.player and not self._slider_is_active:
            try:
                time_pos = self.player.time_pos
                if time_pos is not None:
                    self.on_time_changed('time-pos', time_pos)
            except:
                pass

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

    def _load_video_file(self, file_path):
        """Internal method to handle the actual video loading process"""
        if not self.player:
            print("MPV player not initialized")
            return
            
        try:
            # Reset timeline
            self.timeline.setValue(0)
            self.set_timecode("00:00:00")
            
            # Stop position timer during load
            self.position_timer.stop()
            
            self.video_title = os.path.basename(file_path)
            self.update_window_title()
            self.current_video_path = file_path
            
            # Load the video
            self.player.play(file_path)
            self.player.pause = True  # Start paused
            self.player.time_pos = 0   # Start from beginning
            
            # Enable controls
            self.play_pause_button.setEnabled(True)
            self.back_button.setEnabled(True)
            self.forward_button.setEnabled(True)
            self.timeline.setEnabled(True)
            
            # Start position monitoring
            self.position_timer.start()
            
            # Emit signal
            QTimer.singleShot(500, lambda: self.video_loaded.emit(self.current_video_path))
            
        except Exception as e:
            print(f"Failed to load video: {e}")

    def set_timecode(self, timecode):
        self.timecode = timecode
        self.update_window_title()

    def toggle_play_pause(self):
        if not self.player:
            return
            
        try:
            self.player.pause = self.is_playing
        except:
            pass

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
        if not self.player:
            return
            
        try:
            current_time = self.player.time_pos or 0
            new_position = current_time + seconds
            duration = self.player.duration or 0
            
            # Clamp to valid range
            new_position = max(0, min(new_position, duration))
            self.player.time_pos = new_position
        except:
            pass

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
            
            # Set the timeline slider value
            self.timeline.setValue(ms)
            
            # Update MPV position
            self.update_position_from_slider()
            
            # Update timecode display
            seconds = ms // 1000
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            self.set_timecode(f"{h:02}:{m:02}:{s:02}")
            
            # Emit timecode change signal
            self.emit_timecode_changed(ms)
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
            if self.player:
                self.player.terminate()
        except Exception:
            pass
        super().closeEvent(event)

    def update_position_from_slider(self):
        if self.player:
            try:
                seconds = self.timeline.value() / 1000.0
                self.player.time_pos = seconds
            except:
                pass

    def extract_frames_for_timecodes(self, timecodes):
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
        all_timecodes = timecodes
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
        self.frames_extracted.emit(frames)

class FrameExtractorWorker(QObject):
    finished = pyqtSignal(list)

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
    """Custom slider that supports real-time scrubbing"""
    
    def __init__(self, orientation):
        super().__init__(orientation)
        self.was_playing_before_scrub = False
        self.player_window = None
        self.is_scrubbing = False
        self.last_seek_time = 0
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.player_window and self.player_window.player:
            self.is_scrubbing = True
            
            # Remember if we were playing before scrubbing
            try:
                self.was_playing_before_scrub = not self.player_window.player.pause
            except:
                self.was_playing_before_scrub = False
            
            # Pause immediately
            if self.was_playing_before_scrub:
                try:
                    self.player_window.player.pause = True
                except:
                    pass
            
            # Calculate and jump to clicked position
            self._jump_to_mouse_position(event, immediate=True)
            self.sliderPressed.emit()
        
        super().mousePressEvent(event)
    
    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self.player_window and self.is_scrubbing:
            # Throttle seeking for better performance
            current_time = time.time() * 1000
            if current_time - self.last_seek_time > 50:
                self._jump_to_mouse_position(event, immediate=False)
                self.last_seek_time = current_time
            else:
                self._update_slider_position_only(event)
        
        super().mouseMoveEvent(event)
    
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.player_window and self.is_scrubbing:
            # Jump to final position
            self._jump_to_mouse_position(event, immediate=True)
            
            # Restore previous playback state
            if self.was_playing_before_scrub:
                try:
                    self.player_window.player.pause = False
                except:
                    pass
            
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
        
        value = max(self.minimum(), min(self.maximum(), int(value)))
        self.setValue(value)
        
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
        
        value = max(self.minimum(), min(self.maximum(), int(value)))
        self.setValue(value)
        
        if self.player_window:
            seconds = value // 1000
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            self.player_window.set_timecode(f"{h:02}:{m:02}:{s:02}")
            self.player_window.emit_timecode_changed(value)
            
            # Seek video
            if self.player_window.player:
                try:
                    self.player_window.player.time_pos = value / 1000.0
                except:
                    pass