DEBUG = True  # Add this at the top

from PyQt5.QtCore import Qt, pyqtSignal, QUrl, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSlider
)
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from PyQt5.QtMultimediaWidgets import QVideoWidget

import os
from utility import pct_to_milliseconds, timecode_to_milliseconds, milliseconds_to_timecode, minimum_load_interval

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
        self._pending_timecode = None  # Store timecode to jump to after loading
        self._pending_initial_timecode = None  # Store initial timecode for new videos
        
        # Add loading state management
        self._media_loading = False
        self._pending_load_request = None  # Store requests that come in while loading
        self._last_load_time = 0  # Track when last load started
        self.minimum_load_interval = minimum_load_interval  # Minimum time between loads in seconds
        
        # Add flag to track if we're closing
        self._is_closing = False
        
        # Store timer references to cancel them if needed
        self._pending_timers = []

        # Dual media player setup
        self.current_player_index = 0  # 0 or 1
        self.next_player_index = 1
        self._pending_switch_data = None  # Store data for pending switch
        
        # Set a minimum height for the player window
        self.setMinimumHeight(300)

        # Create two media players
        self.media_players = [
            QMediaPlayer(None, QMediaPlayer.VideoSurface),
            QMediaPlayer(None, QMediaPlayer.VideoSurface)
        ]
        
        # Create video widget (shared between both players)
        self.video_widget = QVideoWidget()
        
        # Set the initial player output
        self.media_players[self.current_player_index].setVideoOutput(self.video_widget)

        if ui.is_dark_mode():
            self.video_widget.setStyleSheet("background-color: #111;")
        else:
            self.video_widget.setStyleSheet("background-color: #eee;")

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
        self.timeline.sliderPressed.connect(self.on_slider_pressed)
        self.timeline.sliderReleased.connect(self.on_slider_released)

        button_width, button_height = self.ui.get_dimensions('button')

        # Play/Pause button
        self.play_pause_button = QPushButton("⏵")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.play_pause_button.setEnabled(False)
        self.play_pause_button.setFont(self.ui.get_font('button'))
        self.play_pause_button.setFixedSize(60, button_height)
        self.is_playing = False

        # Seek back
        self.back_button = QPushButton("⏪")
        self.back_button.setEnabled(False)
        self.back_button.clicked.connect(self.seek_back)
        self.back_button.setFont(self.ui.get_font('button'))
        self.back_button.setFixedSize(60, button_height)

        # Seek forward
        self.forward_button = QPushButton("⏩")
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

        # Setup media player connections for both players
        self._setup_media_connections()

    @property
    def media_player(self):
        """Get the currently active media player"""
        return self.media_players[self.current_player_index]

    @property
    def next_media_player(self):
        """Get the next media player (for loading)"""
        return self.media_players[self.next_player_index]

    def _setup_media_connections(self):
        """Set up all QMediaPlayer signal connections for both players"""
        for i, player in enumerate(self.media_players):
            player.stateChanged.connect(lambda state, idx=i: self._on_state_changed(state, idx))
            player.mediaStatusChanged.connect(lambda status, idx=i: self._on_media_status_changed(status, idx))
            player.durationChanged.connect(lambda duration, idx=i: self._on_duration_changed(duration, idx))
            player.positionChanged.connect(lambda position, idx=i: self._on_position_changed(position, idx))
            player.error.connect(lambda error, idx=i: self._on_error(error, idx))

    def _switch_to_next_player(self):
        """Switch from current player to next player"""
        if DEBUG: print(f"DEBUG: Switching from player {self.current_player_index} to {self.next_player_index}")
        
        # IMPORTANT: Stop and clear the current player BEFORE switching
        current_player = self.media_players[self.current_player_index]
        current_player.stop()
        current_player.setVideoOutput(None)
        if DEBUG: print(f"DEBUG: Stopped and cleared video output for player {self.current_player_index}")
        
        # Switch indices
        self.current_player_index, self.next_player_index = self.next_player_index, self.current_player_index
        
        # Set video output to new current player
        self.media_player.setVideoOutput(self.video_widget)
        
        if DEBUG: print(f"DEBUG: Switch complete - now using player {self.current_player_index}")

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
        if not file_path or not os.path.exists(file_path):
            print(f"Cannot load video: file not found at {file_path}")
            return

        # Check cooldown period - require n seconds between loads
        import time
        current_time = time.time()
        time_since_last_load = abs(current_time - self._last_load_time)

        if time_since_last_load < self.minimum_load_interval:
            if DEBUG: print(f"DEBUG: Load blocked - only {time_since_last_load:.1f}s since last load (need {self.minimum_load_interval:.1f}s)")
            # Store as pending request instead of blocking entirely
            self._pending_load_request = (file_path, metadata, timecode)
            return

        # Check if we're currently loading
        if self._media_loading:
            if DEBUG: print(f"DEBUG: Media player busy loading, REPLACING queued request with: {os.path.basename(file_path)}")
            # Replace any pending request with the new one (latest wins)
            self._pending_load_request = (file_path, metadata, timecode)
            return

        # Check if same video is already loaded on current player
        if hasattr(self, 'current_video_path') and self.current_video_path == file_path:
            if DEBUG: print("DEBUG: Video already loaded, handling timecode only")
            # Handle timecode directly for already loaded video
            if timecode is not None:
                self._jump_to_timecode_direct(timecode)
            else:
                # If no timecode specified, go to beginning
                self.set_video_time(0)
            return

        # Start loading - record timestamp
        self._last_load_time = current_time
        if DEBUG: print(f"DEBUG: Starting media load for: {os.path.basename(file_path)} on next player {self.next_player_index}")
        self._media_loading = True
        self._pending_load_request = None
        self.video_is_loading.emit()
        
        # Store switch data for when loading completes
        self._pending_switch_data = {
            'file_path': file_path,
            'metadata': metadata,
            'timecode': timecode
        }
        
        self._load_video_file(file_path, timecode)

    def _load_video_file(self, file_path, timecode=None):
        """Internal method to handle the actual video loading process"""
        # Load on the next player (not the currently active one)
        next_player = self.next_media_player
        
        # Store pending timecode for when video is ready
        self._pending_timecode = timecode
        
        # Stop next player if it's playing
        next_player.stop()
        
        # Load new media on next player
        media_content = QMediaContent(QUrl.fromLocalFile(file_path))
        next_player.setMedia(media_content)
        
        # Pause next player immediately after loading
        next_player.pause()

    def _on_state_changed(self, state, player_index):
        """Called when media player state changes"""
        # Only respond to events from the current player for UI updates
        if player_index == self.current_player_index:
            if state == QMediaPlayer.PlayingState:
                self.play_pause_button.setText("⏸")
                self.is_playing = True
                if DEBUG: print(f"DEBUG: Player {player_index} playing")
                
            elif state == QMediaPlayer.PausedState:
                self.play_pause_button.setText("⏵")
                self.is_playing = False
                if DEBUG: print(f"DEBUG: Player {player_index} paused")
                
            elif state == QMediaPlayer.StoppedState:
                self.play_pause_button.setText("⏵")
                self.is_playing = False
                if DEBUG: print(f"DEBUG: Player {player_index} stopped")

    def _on_media_status_changed(self, status, player_index):
        """Called when media status changes"""
        if DEBUG: print(f"DEBUG: Player {player_index} status: {status}")
        
        if status == QMediaPlayer.LoadedMedia:
            if DEBUG: print(f"DEBUG: Player {player_index} media loaded - ready for playback")
            
        elif status == QMediaPlayer.BufferedMedia:
            if DEBUG: print(f"DEBUG: Player {player_index} media buffered")
            
            # Only handle buffered event for the next player when we're loading
            if player_index == self.next_player_index and self._media_loading:
                next_player = self.next_media_player
                if next_player.duration() > 0:
                    if DEBUG: print(f"DEBUG: Next player {player_index} buffered with duration - ready for switch")
                    
                    # Jump to timecode on next player if specified
                    if self._pending_timecode is not None:
                        if DEBUG: print(f"DEBUG: Jumping next player to timecode: {self._pending_timecode}")
                        self._jump_to_timecode_on_player(next_player, self._pending_timecode)
                        # Give more time for larger seeks
                        delay = 300 if self._is_large_timecode(self._pending_timecode) else 150
                        QTimer.singleShot(delay, lambda: self._verify_and_switch())
                    else:
                        # No timecode to jump to, switch immediately
                        QTimer.singleShot(100, lambda: self._verify_and_switch())
    
        elif status == QMediaPlayer.InvalidMedia:
            if DEBUG: print(f"DEBUG: Player {player_index} invalid media")
            if player_index == self.next_player_index:
                self._media_loading = False
                self._pending_timecode = None

    def _is_large_timecode(self, timecode):
        """Check if this is a large timecode that needs more time to seek"""
        if '%' in str(timecode):
            # For percentages > 10%, consider it large
            try:
                pct = float(str(timecode).replace('%', ''))
                return pct > 10
            except:
                return False
        else:
            # For direct timecodes, check if > 10 seconds
            if isinstance(timecode, str):
                time_ms = timecode_to_milliseconds(timecode)
                return time_ms and time_ms > 10000
            else:
                return int(timecode) > 10000

    def _jump_to_timecode_on_player(self, player, timecode):
        """Jump to timecode on a specific player"""
        if '%' in str(timecode):
            duration = player.duration()
            if duration > 0:
                time_ms = pct_to_milliseconds(timecode, duration)
                if time_ms is not None:
                    if DEBUG: print(f"DEBUG: Setting player position to {time_ms}ms based on percentage {timecode}")
                    # Simplified approach - just set position directly
                    player.setPosition(time_ms)
        else:
            # Handle direct timecode strings or millisecond values
            if isinstance(timecode, str):
                time_ms = timecode_to_milliseconds(timecode)
            else:
                time_ms = int(timecode)
            
            if time_ms is not None:
                if DEBUG: print(f"DEBUG: Setting player position to {time_ms}ms")
                # Simplified approach - just set position directly
                player.setPosition(time_ms)

    def _verify_and_switch(self):
        """Verify the next player has moved from zero and switch if ready"""
        if self._is_closing or not self._media_loading:
            return
            
        next_player = self.next_media_player
        current_time = next_player.position()
        
        # Determine what position we're expecting
        expected_time = None
        if self._pending_timecode is not None:
            if '%' in str(self._pending_timecode):
                duration = next_player.duration()
                if duration > 0:
                    expected_time = pct_to_milliseconds(self._pending_timecode, duration)
            else:
                if isinstance(self._pending_timecode, str):
                    expected_time = timecode_to_milliseconds(self._pending_timecode)
                else:
                    expected_time = int(self._pending_timecode)
        
        # Simple verification logic
        if expected_time is None:
            # No timecode specified - should be at or near zero
            if DEBUG: print(f"DEBUG: No timecode specified, position is {current_time}ms - proceeding with switch")
            self._execute_switch()
        elif expected_time <= 3000:  # If seeking to first 3 seconds
            # For small timecodes, just verify we've moved from zero
            if current_time > 100:  # Moved at least 100ms from start
                if DEBUG: print(f"DEBUG: Small timecode verified - moved to {current_time}ms (expected around {expected_time}ms)")
                self._execute_switch()
            else:
                # Still at zero, retry
                if not hasattr(self, '_verify_retry_count'):
                    self._verify_retry_count = 0
                
                self._verify_retry_count += 1
                
                if self._verify_retry_count > 5:  # Much shorter retry for simple check
                    if DEBUG: print(f"DEBUG: Small timecode timeout - proceeding anyway (at {current_time}ms)")
                    self._verify_retry_count = 0
                    self._execute_switch()
                else:
                    if DEBUG: print(f"DEBUG: Small timecode not ready - still at {current_time}ms, retrying... ({self._verify_retry_count}/5)")
                    QTimer.singleShot(100, lambda: self._verify_and_switch())
        else:
            # For larger timecodes, verify we've moved significantly from zero
            if current_time > 3000:  # Moved at least 3 seconds from start
                if DEBUG: print(f"DEBUG: Large timecode verified - moved to {current_time}ms (expected {expected_time}ms)")
                self._execute_switch()
            else:
                # Haven't moved far enough yet
                if not hasattr(self, '_verify_retry_count'):
                    self._verify_retry_count = 0
                
                self._verify_retry_count += 1
                
                if self._verify_retry_count > 8:  # Reasonable retry limit
                    if DEBUG: print(f"DEBUG: Large timecode timeout - proceeding anyway (at {current_time}ms, expected {expected_time}ms)")
                    self._verify_retry_count = 0
                    self._execute_switch()
                else:
                    if DEBUG: print(f"DEBUG: Large timecode not ready - at {current_time}ms (need >3000ms), retrying... ({self._verify_retry_count}/8)")
                    QTimer.singleShot(100, lambda: self._verify_and_switch())

    def _execute_switch(self):
        """Execute the actual switch between players"""
        if self._is_closing:
            return
        
        # Reset retry counter
        self._verify_retry_count = 0
            
        # Update metadata and paths
        pending_timecode = None
        if self._pending_switch_data:
            self.current_video_path = self._pending_switch_data['file_path']
            self.video_title = os.path.basename(self.current_video_path)
            self.movie_metadata = self._pending_switch_data['metadata']
            pending_timecode = self._pending_switch_data['timecode']
            self._pending_switch_data = None
        
        # Switch players
        self._switch_to_next_player()
        
        # Reset timeline to prevent position issues
        if self.media_player.duration() > 0:
            self.timeline.setRange(0, self.media_player.duration())
            # If no timecode specified, start at beginning
            if pending_timecode is None:
                if DEBUG: print("DEBUG: No timecode specified, starting at beginning")
                self.media_player.setPosition(0)
                self.timeline.setValue(0)
                self._update_timecode_display(0)
            else:
                # The position should already be set from the previous player
                current_pos = self.media_player.position()
                if DEBUG: print(f"DEBUG: Timecode was specified, current position after switch: {current_pos}ms")
                self.timeline.setValue(current_pos)
                self._update_timecode_display(current_pos)
        else:
            # Duration not ready yet, store for later
            self._pending_initial_timecode = pending_timecode

        # Start playback on new current player
        self.media_player.play()
        
        # Mark loading complete
        self._media_loading = False
        self._pending_timecode = None
        
        # Enable controls
        self.play_pause_button.setEnabled(True)
        self.back_button.setEnabled(True)
        self.forward_button.setEnabled(True)
        self.timeline.setEnabled(True)
        
        # Update UI
        self.update_window_title()
        
        # Process any pending loads
        self._process_pending_load()

    def _jump_to_timecode_direct(self, timecode):
        """Jump to timecode directly - handles both percentage and timecode strings"""
        if DEBUG: print(f"DEBUG: Jumping to timecode: {timecode}")
        
        # Check if we have a valid duration first
        duration = self.media_player.duration()
        if duration <= 0:
            if DEBUG: print(f"DEBUG: No duration available yet, storing timecode for later: {timecode}")
            self._pending_initial_timecode = timecode
            return
        
        if '%' in str(timecode):
            time_ms = pct_to_milliseconds(timecode, duration)
            if time_ms is not None:
                if DEBUG: print(f"DEBUG: Jumping to {time_ms}ms based on percentage {timecode} (duration: {duration}ms)")
                self.set_video_time(time_ms)
                # Use delayed playback
                QTimer.singleShot(50, lambda: self._start_playback_after_seek())
            else:
                if DEBUG: print(f"DEBUG: Cannot jump to percentage - invalid percentage: {timecode}")
        else:
            # Handle direct timecode strings or millisecond values
            if isinstance(timecode, str):
                time_ms = timecode_to_milliseconds(timecode)
                if DEBUG: print(f"DEBUG: Jumping to {time_ms}ms based on timecode {timecode}")
            else:
                if DEBUG: print(f"DEBUG: Jumping to {timecode}ms directly")
                time_ms = int(timecode)
            
            if time_ms is not None:
                if DEBUG: print(f"DEBUG: Jumping to {time_ms}ms")
                self.set_video_time(time_ms)
                # Use delayed playback
                QTimer.singleShot(50, lambda: self._start_playback_after_seek())

    def _start_playback_after_seek(self):
        """Start playback after seeking, with validation"""
        # Check if we're closing
        if self._is_closing:
            if DEBUG: print("DEBUG: Skipping playback start - player is closing")
            return
            
        if DEBUG: print("DEBUG: Starting playback after seek")
        if self.media_player and self.media_player.state() != QMediaPlayer.PlayingState:
            self.media_player.play()
        elif DEBUG:
            print("DEBUG: Already playing, no need to start")

    def _on_duration_changed(self, duration, player_index):
        """Called when media duration is determined"""
        # Only respond to duration changes from current player
        if player_index == self.current_player_index and duration > 0:
            self.timeline.setRange(0, duration)
            self.duration_seconds = duration // 1000
            self.update_window_title()
            if DEBUG: print(f"DEBUG: Duration set: {self.duration_seconds}s for player {player_index}")
            
            # If we have a pending timecode to jump to, handle it now that duration is known
            if hasattr(self, '_pending_initial_timecode') and self._pending_initial_timecode is not None:
                if DEBUG: print(f"DEBUG: Processing pending initial timecode: {self._pending_initial_timecode}")
                self._jump_to_timecode_direct(self._pending_initial_timecode)
                self._pending_initial_timecode = None

    def _on_position_changed(self, position, player_index):
        """Called when playback position changes"""
        # Only respond to position changes from current player
        if player_index == self.current_player_index and not self._slider_is_active:
            self.timeline.setValue(position)
            self._update_timecode_display(position)
            self.emit_timecode_changed(position)

    def _on_error(self, error, player_index):
        """Called when media player encounters an error"""
        if DEBUG: print(f"DEBUG: Media player {player_index} error: {error}")
        if player_index == self.next_player_index:
            self._media_loading = False

    def _process_pending_load(self):
        """Process any pending load requests immediately"""
        if self._pending_load_request is not None:
            if DEBUG: print("DEBUG: Processing pending load request immediately")
            file_path, metadata, timecode = self._pending_load_request
            self._pending_load_request = None
            # Direct call - no timer delays
            self.load_video(file_path, metadata, timecode)

    def toggle_play_pause(self):
        if self.media_player.state() == QMediaPlayer.PlayingState:
            self.media_player.pause()
        else:
            self.media_player.play()

    def seek_back(self):
        if not self.current_video_path:
            return
        seek_amount = SEEK_NORMAL
        self.seek_video(-seek_amount)

    def seek_forward(self):
        if not self.current_video_path:
            return
        seek_amount = SEEK_NORMAL
        self.seek_video(seek_amount)

    def seek_video(self, seconds):
        current_time = self.media_player.position()
        new_time = current_time + int(seconds * 1000)
        self.set_video_time(new_time)

    def set_video_time(self, time_ms):
        time_ms = int(time_ms)
        duration = self.media_player.duration()
        if duration > 0:
            time_ms = max(0, min(time_ms, duration))
        self.media_player.setPosition(time_ms)
        self.timeline.setValue(time_ms)
        self._update_timecode_display(time_ms)
        self.emit_timecode_changed(time_ms)

    def jump_to_timecode(self, timecode, is_last_frame=False):
        """Jump to specific timecode"""
        time_ms = timecode_to_milliseconds(timecode)
        if time_ms is not None:
            self.set_video_time(time_ms)
        else:
            print(f"Invalid timecode format: {timecode}")

    def set_timecode(self, timecode):
        """Set the current timecode value (for internal storage)"""
        if isinstance(timecode, int):
            # Convert milliseconds to timecode string
            self.timecode = milliseconds_to_timecode(timecode)
        else:
            # Store timecode string directly
            self.timecode = timecode

    def get_timecode(self):
        """Get the current timecode"""
        return self.timecode

    def _update_timecode_display(self, time_ms):
        # Use utility function to convert milliseconds to timecode
        current_timecode = milliseconds_to_timecode(time_ms)
        self.set_timecode(current_timecode)
        
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

        cap.release()
        
        # Emit the extracted frames to the annotate window
        if frames:
            self.frames_extracted.emit(frames)

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

    def closeEvent(self, event):
        try:
            if DEBUG: print("DEBUG: Player closeEvent called")
            
            # Set closing flag first to prevent any new operations
            self._is_closing = True
            
            # Cancel any pending timers
            if DEBUG: print("DEBUG: Cancelling pending timers")
            for timer in self._pending_timers:
                try:
                    timer.stop()
                    timer.deleteLater()
                except:
                    pass
            self._pending_timers.clear()
            
            # Reset loading state
            if DEBUG: print("DEBUG: Resetting loading state")
            self._media_loading = False
            self._pending_load_request = None
            self._pending_timecode = None
            self._pending_switch_data = None
            
            # Clean up both media players properly
            for i, player in enumerate(self.media_players):
                if player:
                    if DEBUG: print(f"DEBUG: Cleaning up media player {i}")
                    try:
                        player.stop()
                        player.setVideoOutput(None)
                        # Don't delete the player objects here to avoid segfault
                    except Exception as e:
                        if DEBUG: print(f"DEBUG: Error cleaning up player {i}: {e}")
        
        except Exception as e:
            if DEBUG: print(f"DEBUG: Error during player cleanup: {e}")
            import traceback
            if DEBUG: print(f"DEBUG: Traceback: {traceback.format_exc()}")
        finally:
            if DEBUG: print("DEBUG: In finally block, calling parent closeEvent")
            try:
                # Call parent closeEvent
                super().closeEvent(event)
                if DEBUG: print("DEBUG: Parent closeEvent completed successfully")
            except Exception as e:
                if DEBUG: print(f"DEBUG: Error in parent closeEvent: {e}")
                import traceback
                if DEBUG: print(f"DEBUG: Parent closeEvent traceback: {traceback.format_exc()}")

    def on_preferences_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {}

    def on_preferences_load(self, data):
        pass

    def clear_project(self):
        """Clear current project - unload video and reset state"""
        if DEBUG: print("DEBUG: Player clearing project - unloading video")
        
        # Stop both players properly
        for i, player in enumerate(self.media_players):
            if player:
                try:
                    player.stop()
                    player.setVideoOutput(None)
                    if DEBUG: print(f"DEBUG: Cleared project - stopped player {i}")
                except Exception as e:
                    if DEBUG: print(f"DEBUG: Error stopping player {i}: {e}")
    
        # Reset loading state and cooldown
        self._media_loading = False
        self._pending_load_request = None
        self._pending_timecode = None
        self._pending_switch_data = None
        self._pending_initial_timecode = None
        self._last_load_time = 0
        
        # Reset retry counter
        if hasattr(self, '_verify_retry_count'):
            self._verify_retry_count = 0
    
        # Reset player indices
        self.current_player_index = 0
        self.next_player_index = 1
        
        # Set video output back to first player
        if self.media_players[0]:
            self.media_players[0].setVideoOutput(self.video_widget)
    
        # Reset all state
        self.current_video_path = None
        self.video_title = ""
        self.movie_metadata = None
        self.duration_seconds = None
        
        # Reset UI properly
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
        self.was_playing_before_scrub = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.is_scrubbing = True
            
            # Remember if we were playing and pause if so
            if self.player_window:
                self.was_playing_before_scrub = self.player_window.is_playing
                if self.was_playing_before_scrub:
                    self.player_window.media_player.pause()
                    if DEBUG: print("DEBUG: Paused for scrubbing")
            
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
            
            # Restore playback state if we were playing before
            if self.player_window and self.was_playing_before_scrub:
                self.player_window.media_player.play()
                if DEBUG: print("DEBUG: Resumed playback after scrubbing")
            
            self.was_playing_before_scrub = False
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
            # Always update video time during scrubbing for immediate feedback
            self.player_window.set_video_time(value)