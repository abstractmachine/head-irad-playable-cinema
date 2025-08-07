DEBUG = True  # Add this at the top

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QSizePolicy, QSlider
)

import os
import platform
import vlc
from utility import timecode_to_milliseconds, milliseconds_to_timecode

SEEK_NORMAL = 1
SEEK_FAST = 30

class AbstractPlayerWindow(QMainWindow):
    # Signals for saving/loading preferences on application close/open
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)
    # Signals for communication
    video_is_loading = pyqtSignal()
    video_did_load = pyqtSignal(str, dict)
    # Signals for Image Extraction
    frames_extracted = pyqtSignal(list)
    # Signals for playback
    video_timecode_changed = pyqtSignal(int)

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
        self._pending_timecode = None  # Store timecode to jump to when ready

        # Set a minimum height for the player window
        self.setMinimumHeight(300)

        # VLC setup (subclasses may override for other backends)
        os.environ["VLC_VERBOSE"] = str("-1")
        self.vlc_instance = vlc.Instance()
        self.vlc_player = self.vlc_instance.media_player_new()
        self.video_widget = QWidget()

        if ui.is_dark_mode():
            self.video_widget.setStyleSheet("background-color: 111;")
        else:
            self.video_widget.setStyleSheet("background-color: eee;")

        # Timeline slider
        self.timeline = JumpSlider(Qt.Horizontal)
        self.timeline.player_window = self
        self.timeline.setRange(0, 100)
        self.timeline.setValue(0)
        self.timeline.setEnabled(False)
        self.timeline.setMinimumHeight(32)
        # Set margins and spacing to zero, but add left/right margin via stylesheet
        self.timeline.setContentsMargins(0, 0, 0, 0)
        self.timeline.setStyleSheet("QSlider { margin-left: 8px; margin-right: 8px; padding: 0px; }")
        # self.timeline.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # self.timeline.setToolTip("Scrub through the video timeline")
        self.timeline.sliderPressed.connect(self.on_slider_pressed)
        self.timeline.sliderReleased.connect(self.on_slider_released)

        button_width, button_height = self.ui.get_dimensions('button')

        # Play/Pause button
        self.play_pause_button = QPushButton("⏵")
        # self.play_pause_button.setToolTip("Play or pause video\nShortcut:Space")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.play_pause_button.setEnabled(False)
        self.play_pause_button.setFont(self.ui.get_font('button'))
        self.play_pause_button.setFixedSize(60, button_height)
        self.is_playing = False

        # Seek back
        self.back_button = QPushButton("⏪")
        # self.back_button.setToolTip("Seek backward\nShortcut: Left arrow, Shift for fast")
        self.back_button.setEnabled(False)
        self.back_button.clicked.connect(self.seek_back)
        self.back_button.setFont(self.ui.get_font('button'))
        self.back_button.setFixedSize(60, button_height)

        # Seek forward
        self.forward_button = QPushButton("⏩")
        # self.forward_button.setToolTip("Seek forward\nShortcut: Right arrow, Shift for fast")
        self.forward_button.setEnabled(False)
        self.forward_button.clicked.connect(self.seek_forward)
        self.forward_button.setFont(self.ui.get_font('button'))
        self.forward_button.setFixedSize(60, button_height)

        # Timecode display
        self.timecode_label = QLabel("00:00:00 | 00:00:00")
        self.timecode_label.setFont(self.ui.get_font('monospace'))
        self.timecode_label.setAlignment(Qt.AlignCenter)
        self.timecode_label.setStyleSheet("QLabel { padding: 0px; color: #888; }")
        self.timecode_label.setToolTip("Current timecode | Total duration")

        # Layouts
        controls_layout = QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)  # No margins
        controls_layout.setSpacing(0)                   # No spacing
        controls_layout.addStretch()  # Stretch to the left of buttons
        controls_layout.addWidget(self.play_pause_button)
        controls_layout.addWidget(self.back_button)
        controls_layout.addWidget(self.forward_button)
        controls_layout.addStretch()  # Stretch between buttons and timecode label
        controls_layout.addWidget(self.timecode_label)
        controls_layout.addStretch()  # Stretch to the right of

        layout = QVBoxLayout()
        layout.setSpacing(0)                             # No spacing
        layout.setContentsMargins(0, 0, 0, 10)           # No margins
        layout.addWidget(self.video_widget, stretch=1)
        layout.addWidget(self.timeline)
        layout.addLayout(controls_layout)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Remove duration polling timer - we'll use VLC events instead
        # self.duration_timer = None

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

    def load_video(self, file_path, metadata=None, timecode=None):
        """Simple method for loading video with metadata and optional timecode"""
        if file_path and os.path.exists(file_path):
            self.video_is_loading.emit()
            if hasattr(self, 'current_video_path') and self.current_video_path == file_path:
                if DEBUG: print("DEBUG: Video already loaded, skipping reload")
                return
            
            # Store pending timecode for when video is ready
            self._pending_timecode = timecode
            
            self.movie_metadata = metadata
            self._load_video_file(file_path)
        else:
            print(f"Cannot load video: file not found at {file_path}")

    def _load_video_file(self, file_path):
        """Internal method to handle the actual video loading process"""
        self.timeline.setValue(0)
        self.set_timecode("00:00:00")
        
        # Stop current video if playing
        if hasattr(self, 'vlc_player') and self.vlc_player:
            self.vlc_player.stop()
            
        self.video_title = os.path.basename(file_path)
        self.update_window_title()
        
        # Load new media
        media = self.vlc_instance.media_new(file_path)
        self.vlc_player.set_media(media)
        
        # Set video output window
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
        self.play_pause_button.setText("⏵")
        self.is_playing = False

        # Set up VLC events BEFORE playing
        self._setup_vlc_events()
        
        # Start playing to initialize the media, then pause immediately
        self.vlc_player.play()

    def _setup_vlc_events(self):
        """Set up all VLC event handlers"""
        event_manager = self.vlc_player.event_manager()
        
        # Media events
        event_manager.event_attach(vlc.EventType.MediaPlayerLengthChanged, self._on_duration_changed)
        event_manager.event_attach(vlc.EventType.MediaPlayerPositionChanged, self._on_position_changed)
        event_manager.event_attach(vlc.EventType.MediaPlayerPlaying, self._on_video_playing)
        event_manager.event_attach(vlc.EventType.MediaPlayerPaused, self._on_video_paused)
        
        # Time events
        event_manager.event_attach(vlc.EventType.MediaPlayerTimeChanged, self._on_vlc_time_changed)

    def _on_duration_changed(self, event):
        """Called when VLC determines the video duration"""
        duration = self.vlc_player.get_length()
        if duration and duration > 0:
            self.timeline.setRange(0, duration)
            self.duration_seconds = duration // 1000
            self.update_window_title()
            if DEBUG: print(f"DEBUG: Duration set: {self.duration_seconds}s")

    def _on_position_changed(self, event):
        """Called when VLC position changes - video is ready for seeking"""
        if hasattr(self, '_pending_timecode') and self._pending_timecode is not None:
            if DEBUG: print(f"DEBUG: Position changed, video ready for seeking")
            # Video is ready, pause it immediately
            self.vlc_player.pause()
            # Handle pending timecode
            self._handle_pending_timecode()

    def _on_video_playing(self, event):
        """Called when video starts playing"""
        self.play_pause_button.setText("⏸")
        self.is_playing = True
        if DEBUG: print("DEBUG: Video playing")

    def _on_video_paused(self, event):
        """Called when video is paused"""
        self.play_pause_button.setText("⏵")
        self.is_playing = False
        if DEBUG: print("DEBUG: Video paused")

    def _handle_pending_timecode(self):
        """Handle timecode jump when video is ready"""
        if self._pending_timecode is not None:
            if DEBUG: print(f"DEBUG: Jumping to pending timecode: {self._pending_timecode}")
            
            timecode = self._pending_timecode
            self._pending_timecode = None  # Clear pending timecode
            
            # Jump to timecode
            if isinstance(timecode, str):
                self.jump_to_timecode(timecode)
            elif isinstance(timecode, (int, float)):
                self.set_video_time(int(timecode))
            
            # Emit that video is loaded
            self.video_did_load.emit(self.current_video_path, self.movie_metadata)

    def toggle_play_pause(self):
        if self.vlc_player.is_playing():
            self.vlc_player.pause()
        else:
            self.vlc_player.play()

    def seek_back(self):
        if not self.current_video_path or not self.vlc_player:
            return
        seek_amount = SEEK_NORMAL
        self.seek_video(-seek_amount)

    def seek_forward(self):
        if not self.current_video_path or not self.vlc_player:
            return
        seek_amount = SEEK_NORMAL
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
        self.set_timecode(time_ms)
        self.emit_timecode_changed(time_ms)

    def jump_to_timecode(self, timecode, is_last_frame=False):
        """Jump to specific timecode"""
        time_ms = timecode_to_milliseconds(timecode)
        if time_ms is not None:
            self.set_video_time(time_ms)
        else:
            print(f"Invalid timecode format: {timecode}")

    def set_timecode(self, timecode):
        """Set the current timecode display"""
        self.timecode = timecode

    def get_timecode(self):
        """Get the current timecode"""
        return self.timecode

    def _update_timecode_display(self, time_ms):
        # Use utility function to convert milliseconds to timecode
        current_timecode = milliseconds_to_timecode(time_ms)
        self.set_timecode(current_timecode)  # This calls the method above
        
        duration_seconds = getattr(self, "duration_seconds", None)
        if duration_seconds is not None and duration_seconds > 0:
            duration_timecode = milliseconds_to_timecode(duration_seconds * 1000)
            self.timecode_label.setText(f"{current_timecode} | {duration_timecode}")
        else:
            self.timecode_label.setText(f"{current_timecode} | 00:00:00")

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
            # Convert timecode to milliseconds using utility function
            time_ms = timecode_to_milliseconds(timecode)
            if time_ms is not None:
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
        """Called when VLC time position changes"""
        if not self._slider_is_active:  # Don't update during scrubbing
            position = self.vlc_player.get_time()
            self.timeline.setValue(position)
            self.set_timecode(position)
            self.emit_timecode_changed(position)

    def closeEvent(self, event):
        try:
            if hasattr(self, 'vlc_player') and self.vlc_player:
                self.vlc_player.stop()
                # Detach all events
                event_manager = self.vlc_player.event_manager()
                if event_manager:
                    event_manager.event_detach(vlc.EventType.MediaPlayerTimeChanged)
                    event_manager.event_detach(vlc.EventType.MediaPlayerPlaying)
                    event_manager.event_detach(vlc.EventType.MediaPlayerPaused)
                    event_manager.event_detach(vlc.EventType.MediaPlayerLengthChanged)
                    event_manager.event_detach(vlc.EventType.MediaPlayerPositionChanged)
                self.vlc_player.release()
                self.vlc_player = None
            if hasattr(self, 'vlc_instance') and self.vlc_instance:
                self.vlc_instance.release()
                self.vlc_instance = None
        except Exception as e:
            print(f"Error during cleanup: {e}")
        finally:
            super().closeEvent(event)

    def on_preferences_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {}

    def on_preferences_load(self, data):
        pass

    def clear_project(self):
        """Clear current project - unload video and reset state"""
        if DEBUG: print("DEBUG: Player clearing project - unloading video")
        
        # Stop and clear current video
        if hasattr(self, 'vlc_player') and self.vlc_player:
            self.vlc_player.stop()
        
        # Reset all state
        self.current_video_path = None
        self.video_title = ""
        self.movie_metadata = None
        self.duration_seconds = None
        self._pending_timecode = None
        
        # Reset UI
        self.timeline.setValue(0)
        self.timeline.setRange(0, 100)
        self.timeline.setEnabled(False)
        self.set_timecode("00:00:00")
        self._update_timecode_display(0)
        
        # Disable controls
        self.play_pause_button.setEnabled(False)
        self.back_button.setEnabled(False)
        self.forward_button.setEnabled(False)
        self.play_pause_button.setText("⏵")
        self.is_playing = False
        
        # Update window title
        self.update_window_title()

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
                self.player_window.set_timecode(value)
                self.player_window.emit_timecode_changed(value)